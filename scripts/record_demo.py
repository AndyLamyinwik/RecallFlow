"""Record actual browser routes using isolated, explicitly synthetic demo data.

Install requirements-demo.txt and run `python -m playwright install ffmpeg`.
Chrome is used when installed; --browser-executable can select another Chromium.
Nothing is uploaded. No human participants, quotes, or human task times exist.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

from scripts.demo_app import DEMO_NOW, create_demo_environment
from scripts.generate_demo_usage import csrf


ROOT = Path(__file__).resolve().parents[1]
CHAPTERS = (("open", 34), ("later", 16), ("dismiss", 22), ("architecture", 10))
OVERLAY = """(() => {
  document.addEventListener('DOMContentLoaded', () => {
    if (!document.body) return;
    const style = document.createElement('style');
    style.textContent = `html{zoom:.88} body{padding-bottom:130px!important}
    #rf-demo-caption{position:fixed;bottom:0;left:0;right:0;z-index:100000;
    background:#173041;color:white;padding:16px 30px;font:20px/1.5 'Microsoft JhengHei',sans-serif;
    border-top:4px solid #63c4af;box-shadow:0 -3px 18px #0002;}
    #rf-demo-caption small{display:block;color:#9fdbc9;font-size:14px;letter-spacing:.5px;}`;
    document.head.appendChild(style);
    const footer = document.createElement('div'); footer.id = 'rf-demo-caption';
    const small = document.createElement('small');
    small.textContent = 'DEMO DATA · 2026-10-26 UTC · scripted scenarios / mock network · not human research';
    const line = document.createElement('div'); line.id = 'rf-demo-line';
    line.textContent = sessionStorage.getItem('rf-demo-caption') || 'RecallFlow — 讓收藏在合適的日期重新出現';
    footer.append(small, line); document.body.appendChild(footer);
  });
})();"""


def caption(page, text: str) -> None:
    page.evaluate("""text => {
      sessionStorage.setItem('rf-demo-caption', text);
      const line=document.getElementById('rf-demo-line'); if(line) line.textContent=text;
    }""", text)


def media_duration(ffmpeg: str, path: Path) -> float:
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not match:
        raise RuntimeError("Cannot read video duration")
    hours, minutes, seconds = map(float, match.groups())
    return hours * 3600 + minutes * 60 + seconds


def assemble(clips: list[Path], output: Path) -> tuple[float, str]:
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    arguments = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n"]
    filters = []
    for index, (clip, (_, target)) in enumerate(zip(clips, CHAPTERS, strict=True)):
        arguments += ["-i", str(clip)]
        duration = media_duration(ffmpeg, clip)
        filters.append(
            f"[{index}:v]setpts={(target / duration):.9f}*(PTS-STARTPTS),"
            f"fps=25,scale=1280:900,setsar=1,tpad=stop_mode=clone:stop_duration=1,"
            f"trim=duration={target}[v{index}]"
        )
    filters.append("".join(f"[v{i}]" for i in range(len(clips))) + "concat=n=4:v=1:a=0[out]")
    arguments += ["-filter_complex", ";".join(filters), "-map", "[out]", "-c:v", "libx264",
                  "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags",
                  "+faststart", "-an", str(output)]
    subprocess.run(arguments, check=True, capture_output=True)
    return media_duration(ffmpeg, output), ffmpeg


def run(args: argparse.Namespace) -> None:
    import uvicorn
    from playwright.sync_api import sync_playwright, expect
    from sqlalchemy import select

    output = args.output.resolve()
    poster = output.with_name("demo-poster.png" if output.name == "recallflow-demo.mp4"
                              else output.stem + "-poster.png")
    if any(path.exists() for path in (output, output.with_suffix(".json"), poster)):
        raise ValueError("Refusing to overwrite existing demo artifacts; choose a new --output")
    output.parent.mkdir(parents=True, exist_ok=True)
    exports = ROOT / "exports"
    exports.mkdir(exist_ok=True)
    raw_dir = Path(tempfile.mkdtemp(prefix="recording-demo-", dir=exports))
    base = f"http://127.0.0.1:{args.port}"
    # Production URL policy permits only default source ports. Keep that policy
    # intact: the browser fulfills this exact synthetic fixture URL locally.
    source_base = "http://demo.recallflow.test"
    env = create_demo_environment(args.database, source_base_url=source_base)
    server = uvicorn.Server(uvicorn.Config(env.app, host="127.0.0.1", port=args.port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    ready_deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < ready_deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("The isolated demo server did not start")

    from app.models import ActionEvent, ResurfacingEvent, SavedItem
    checks: list[dict] = []
    clips: list[Path] = []
    screenshots: list[Path] = []
    try:
        with sync_playwright() as playwright:
            options = {"headless": True, "slow_mo": 120,
                       "args": ["--no-proxy-server"]}
            if args.browser_executable:
                options["executable_path"] = str(args.browser_executable)
            else:
                options["channel"] = "chrome"
            browser = playwright.chromium.launch(**options)
            for case, target_seconds in CHAPTERS:
                print(f"Recording DEMO chapter: {case}", flush=True)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    record_video_dir=raw_dir, record_video_size={"width": 1280, "height": 900},
                    timezone_id="UTC", locale="en-US",
                )
                context.add_init_script(OVERLAY)
                def fulfill_demo_source(route) -> None:
                    response = context.request.get(base + urlsplit(route.request.url).path)
                    route.fulfill(response=response)

                context.route("http://demo.recallflow.test/demo/source/**", fulfill_demo_source)
                # Authenticate with the normal CSRF-bound route before opening
                # a recorded page. Random demo codes are never in the video.
                if case != "architecture":
                    home = context.request.get(base + "/")
                    logged_in = context.request.post(base + "/session", form={
                        "access_code": env.access_codes[case], "timezone_name": "UTC",
                        "csrf_token": csrf(home.text()),
                    }, max_redirects=0)
                    if logged_in.status != 303:
                        raise RuntimeError("Demo route login failed")
                page = context.new_page()
                page.set_default_timeout(15000)
                started = time.monotonic()
                if case == "architecture":
                    page.goto(base + "/demo/architecture", wait_until="networkidle")
                    page.wait_for_timeout(800)
                    shot = raw_dir / "architecture.png"
                    page.screenshot(path=shot)
                    screenshots.append(shot)
                else:
                    page.goto(base + "/items", wait_until="networkidle")
                    expect(page.locator("#rf-demo-caption")).to_be_visible()
                    caption(page, f"示範帳號 { {'open':'A','later':'B','dismiss':'C'}[case] } · 每個帳號每天最多一張新卡片")
                    page.wait_for_timeout(1300 if case == "open" else 300)
                    page.get_by_role("link", name="Save an article", exact=True).click()
                    caption(page, "儲存公開文章連結，選擇回訪日期，留下收藏原因。")
                    page.get_by_label("Public article URL").fill(source_base + f"/demo/source/{case}")
                    page.get_by_label("Learn from it").check()
                    page.get_by_label("Bring it back on").fill(DEMO_NOW.date().isoformat())
                    page.get_by_label("Optional note").fill("DEMO: Bring this idea back when I can use it.")
                    page.wait_for_timeout(2200 if case == "open" else 200)
                    page.get_by_role("button", name="Save article", exact=True).click()
                    expect(page.get_by_role("heading", name=f"DEMO — Reconnect with useful knowledge ({case})")).to_be_visible()
                    if case == "open":
                        caption(page, "文章已儲存。選擇示範日 2026-10-26，讓排程立即可見。")
                        page.wait_for_timeout(2500)
                    page.goto(base + "/items", wait_until="networkidle")
                    card = page.locator("[data-resurfacing-card]")
                    expect(card).to_have_count(1)
                    expect(page.get_by_role("button", name="Open", exact=True)).to_be_enabled()
                    card.scroll_into_view_if_needed()
                    caption(page, "回訪日期已到：卡片說明 Why now；重新整理不會增加曝光。")
                    page.wait_for_timeout(4000 if case == "open" else 700)
                    with env.factory() as database:
                        before = Counter(e.event_name.value for e in database.scalars(select(ActionEvent)))
                    page.reload(wait_until="networkidle")
                    expect(page.locator("[data-resurfacing-card]")).to_have_count(1)
                    with env.factory() as database:
                        after = Counter(e.event_name.value for e in database.scalars(select(ActionEvent)))
                    assert before["resurfacing_impression"] == after["resurfacing_impression"]
                    if case == "open":
                        caption(page, "Open：完成連結檢查並交接至原文；不表示已閱讀。")
                        page.get_by_role("button", name="Open", exact=True).click()
                        expect(page.get_by_role("heading", name="DEMO — Reconnect with useful knowledge", exact=True)).to_be_visible()
                        caption(page, "已導向合成示範來源。網路檢查為模擬結果，並非真實連線測試。")
                        page.wait_for_timeout(3000)
                        shot = raw_dir / "open.png"
                        page.screenshot(path=shot)
                        screenshots.append(shot)
                    elif case == "later":
                        caption(page, "示範帳號 B · Later：現在沒空，改成三天後。")
                        page.get_by_text("Later", exact=True).click()
                        page.wait_for_timeout(1200)
                        page.get_by_role("button", name="3 days", exact=False).click()
                        expect(page.locator("[data-resurfacing-card]")).to_have_count(0)
                        expect(page.locator(".item-meta")).to_contain_text("2026-10-29")
                        caption(page, "已改為 2026-10-29。今天的卡片已處理，文章仍保留。")
                        page.wait_for_timeout(2200)
                        shot = raw_dir / "later.png"
                        page.screenshot(path=shot)
                        screenshots.append(shot)
                    else:
                        caption(page, "示範帳號 C · Dismiss：停止浮現；30 秒內可以 Undo。")
                        page.get_by_role("button", name="Dismiss", exact=True).click()
                        expect(page.get_by_role("button", name="Undo Dismiss", exact=True)).to_be_visible()
                        expect(page.locator(".item-meta")).to_contain_text("Dismissed")
                        page.wait_for_timeout(1500)
                        page.get_by_role("button", name="Undo Dismiss", exact=True).click()
                        expect(page.locator("[data-resurfacing-card]")).to_have_count(1)
                        caption(page, "Undo 已復原原卡片；不新增第二次曝光。")
                        page.wait_for_timeout(1600)
                        page.get_by_role("button", name="Pause resurfacing", exact=True).click()
                        expect(page.get_by_role("button", name="Resume resurfacing", exact=True)).to_be_visible()
                        caption(page, "也可以暫停全部浮現。文章與回訪日期保持不變。")
                        page.wait_for_timeout(1400)
                        page.get_by_role("button", name="Resume resurfacing", exact=True).click()
                        expect(page.get_by_role("button", name="Pause resurfacing", exact=True)).to_be_visible()
                        expect(page.locator("[data-resurfacing-card]")).to_have_count(0)
                        caption(page, "已恢復；不會為當天已使用的名額額外補一張卡片。")
                        shot = raw_dir / "dismiss-undo-pause.png"
                        page.screenshot(path=shot)
                        screenshots.append(shot)
                    checks.append({"scenario": case, "result": "PASS", "refresh_no_duplicate_impression": True})
                elapsed = time.monotonic() - started
                page.wait_for_timeout(max(300, int((target_seconds - elapsed) * 1000)))
                video = page.video
                page.close()
                context.close()
                clips.append(Path(video.path()))
            browser.close()
        duration, ffmpeg = assemble(clips, output)
        if not 60 <= duration <= 90:
            raise RuntimeError("Final video is outside the requested 60–90 second range")
        with env.factory() as database:
            events = Counter(e.event_name.value for e in database.scalars(select(ActionEvent)))
            cards = database.scalars(select(ResurfacingEvent)).all()
            items = database.scalars(select(SavedItem)).all()
            assert len(cards) == 3 and events["resurfacing_impression"] == 3
            assert events["source_opened"] == 1 and events["later_confirmed"] == 1
            assert events["dismissed"] == 1 and events["dismiss_undone"] == 1
            assert events["pause_enabled"] == events["pause_disabled"] == 1
            assert len(items) == 3
        manifest = {
            "data_kind": "demo", "evaluation_type": "internal automated scenario walkthrough",
            "human_participants": 0, "network": "mocked extraction and reachability; local synthetic source",
            "fixed_clock_utc": DEMO_NOW.isoformat(), "duration_seconds": round(duration, 2),
            "resolution": "1280x900", "audio": "none; burned-in Traditional Chinese captions",
            "chapters": [{"name": name, "duration_seconds": seconds} for name, seconds in CHAPTERS],
            "checks": checks, "event_counts": dict(sorted(events.items())),
        }
        output.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-ss", "10", "-i",
                        str(output), "-frames:v", "1", str(poster)], check=True, capture_output=True)
        report_dir = ROOT / "reports"
        report_dir.mkdir(exist_ok=True)
        lines = ["# RecallFlow 內部模擬走查", "", "> 自動化情境檢查；沒有真人參與者，亦非真人可用性研究。", "",
                 f"執行日期：{datetime.now(timezone.utc).date().isoformat()}（UTC）；示範時鐘：{DEMO_NOW.isoformat()}。", "",
                 "使用隔離的本機 SQLite、三個示範帳號與合成文章，透過真實瀏覽器及現有 HTTP 路由操作。",
                 "正文抽取與連結可達性是注入的模擬結果，因此不能以此證明外部網站擷取或安全檢查效果。", "",
                 "| 情境 | 已檢查的實際行為 | 結果 |", "| --- | --- | --- |",
                 "| A：保存與 Open | 保存連結與日期、展示一張到期卡、刷新不重複曝光、交接到合成來源 | PASS |",
                 "| B：Later | 延後三天，更新日期，當前卡片完成處理 | PASS |",
                 "| C：Dismiss／Undo／Pause | 停止浮現、復原原卡片、暫停與恢復、每日名額保持限制 | PASS |", "",
                 "## 事件核對", "", "| 事件 | 筆數 |", "| --- | ---: |"]
        lines += [f"| `{name}` | {count} |" for name, count in sorted(events.items())]
        lines += ["", "3 個帳號共產生 3 張卡片與 3 次曝光，刷新及 Undo 沒有重複曝光。", "",
                  "## 能證明與不能證明的事", "",
                  "這些檢查證明上述腳本在目前版本可完成指定狀態流轉，不能代替陌生使用者的理解程度、任務耗時、偏好或長期留存。",
                  "影片各章的停留時間是剪輯安排，不是真人任務耗時；沒有參與者引述或自行填造的滿意度。", "",
                  "檢查發現保存流程缺少結果事件；本輪補上真實 capture_started／succeeded／failed、重試去重與測試。",
                  "瀏覽器走查也發現尚未決定的卡片會在刷新後消失；已修正為保留同一張卡片及可用操作，並驗證曝光仍只記一次。",
                  "尚未根據真人回饋修改介面。真人測試計畫保留在 [測試計畫](../docs/usability-test-plan.md)。", "",
                  f"影片：[82 秒演示](../{output.relative_to(ROOT).as_posix()})；[錄製中繼資料](../{output.with_suffix('.json').relative_to(ROOT).as_posix()})。", ""]
        (report_dir / "internal_walkthrough.md").write_text("\n".join(lines), encoding="utf-8")
        print(json.dumps({"video": str(output), "duration_seconds": duration,
                          "walkthrough": "PASS", "raw_recordings": str(raw_dir)}, ensure_ascii=False), flush=True)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        env.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="A NEW ignored data/*demo*.db")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/media/recallflow-demo.mp4")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--browser-executable", type=Path)
    args = parser.parse_args()
    if not args.database.resolve().is_relative_to(ROOT / "data"):
        parser.error("Demo databases must stay within the ignored project data/ directory")
    if not args.output.resolve().is_relative_to(ROOT):
        parser.error("Keep the demo artifact within this project")
    run(args)


if __name__ == "__main__":
    main()
