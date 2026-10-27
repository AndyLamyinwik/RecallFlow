# RecallFlow 內部模擬走查

> 自動化情境檢查；沒有真人參與者，亦非真人可用性研究。

執行日期：2026-10-27（UTC）；示範時鐘：2026-10-26T09:00:00+00:00。

使用隔離的本機 SQLite、三個示範帳號與合成文章，透過真實瀏覽器及現有 HTTP 路由操作。
正文抽取與連結可達性是注入的模擬結果，因此不能以此證明外部網站擷取或安全檢查效果。

| 情境 | 已檢查的實際行為 | 結果 |
| --- | --- | --- |
| A：保存與 Open | 保存連結與日期、展示一張到期卡、刷新不重複曝光、交接到合成來源 | PASS |
| B：Later | 延後三天，更新日期，當前卡片完成處理 | PASS |
| C：Dismiss／Undo／Pause | 停止浮現、復原原卡片、暫停與恢復、每日名額保持限制 | PASS |

## 事件核對

| 事件 | 筆數 |
| --- | ---: |
| `capture_started` | 3 |
| `capture_succeeded` | 3 |
| `dismiss_undone` | 1 |
| `dismissed` | 1 |
| `later_confirmed` | 1 |
| `open_attempt` | 1 |
| `pause_disabled` | 1 |
| `pause_enabled` | 1 |
| `resurfacing_impression` | 3 |
| `source_opened` | 1 |

3 個帳號共產生 3 張卡片與 3 次曝光，刷新及 Undo 沒有重複曝光。

## 能證明與不能證明的事

這些檢查證明上述腳本在目前版本可完成指定狀態流轉，不能代替陌生使用者的理解程度、任務耗時、偏好或長期留存。
影片各章的停留時間是剪輯安排，不是真人任務耗時；沒有參與者引述或自行填造的滿意度。

檢查發現保存流程缺少結果事件；本輪補上真實 capture_started／succeeded／failed、重試去重與測試。
瀏覽器走查也發現尚未決定的卡片會在刷新後消失；已修正為保留同一張卡片及可用操作，並驗證曝光仍只記一次。
尚未根據真人回饋修改介面。真人測試計畫保留在 [測試計畫](../docs/usability-test-plan.md)。

影片：[82 秒演示](../docs/media/recallflow-demo.mp4)；[錄製中繼資料](../docs/media/recallflow-demo.json)。
