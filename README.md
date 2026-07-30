# AI_SubtitleMatcher
利用 AI 協助比對兩份字幕，將 **A 字幕的文字** 配上 **B 字幕的時間軸**。

適合：

- 同一作品不同場次
- 同一作品不同版本
- 已校對字幕套用到另一份字幕
- 人工校對前的快速配對

本程式提供 AI 配對、人工校正、播放器同步確認，以及多對一字幕合併等功能，大幅減少重複校對字幕所需的時間。

---

# 功能特色

- 支援 SRT 字幕
- 支援多種 AI 模型
  - Gemini
  - OpenAI
  - Anthropic
  - DeepSeek
  - OpenRouter
  - Ollama
  - OpenAI Compatible API
- AI 配對結果自動檢查
- AI 檢查報告
  - AI 未回傳
  - 疑似異常配對
  - 順序異常
- 配對衝突檢查
- 無對應 A / B 快速瀏覽
- 內建 VLC 播放器同步確認字幕
- 自動儲存專案

---

# 系統需求

- Windows 10 / 11 （非windows電腦不確定是否可正常執行程式檔）
- [VLC Media Player](https://images.videolan.org/index.zh_TW.html)（若需要播放影片）
- 網路（使用雲端 AI 時）

---

# 安裝方式

## 方法一（建議）

下載 Release 中的最新版：

```
Subtitle_Matcher_x.x.x.exe
```

直接執行即可。

若要使用影片播放功能，請先安裝 VLC Media Player。

---

## 方法二（Python）

安裝 requirements：

```bash
pip install -r requirements.txt
```

執行：

```bash
python subtitle_matcher_gui_x_x_x_vlc.py
```

---

# 使用流程

![](https://imgur.com/uUMn0kB.jpg)

1. 載入 A 字幕、載入 B 字幕
2. 視需要載入B影片或音檔
3. 設定 API Key 與模型
   1. ![](https://imgur.com/emAz0QF.jpg)

4. 執行 AI 配對：若要整部配對則選擇「分段配對全部」，局部配對則圈選出A以及對應的B字幕後按「配對所選範圍」。
5. 查看 **AI 檢查報告**： ps若中途有刪除新增字幕不影響報告「跳到這裡」的功能。
   ![](https://imgur.com/x5FDiya.jpg)
6. **如何配對：左邊A字幕點取、再點相對應B字幕後，按下enter即可**。可以A選多條配對單一B字幕時間(A字幕會合併成上下行呈現)，或者單A一條字幕，配對多條相鄰B字幕時間(時間會合併)。
7. 修正 **配對衝突**：每一筆務必都要處理。請先按「配對衝突」按紐會自動跳轉。第一步都是先「解除配對」
8. 修正 **無對應 A**：當A無任何配對到B時，需確認。
9. 修正 **無對應 B**：當B無任何配對到B時，需確認。
10. 重要：全選 A，按取消 B 的選取按紐，執行 **合併多A→單B**。這樣才會A字幕合併成上下行呈現，若不合併在字幕或播放軟體呈現順序會不一致。
11. 輸出 SRT
12. 使用字幕軟體做最後確認。

---

# AI 檢查報告

AI 配對完成後，建議優先檢查：

- AI 未回傳
- 疑似異常配對
- 順序異常

完成後再開始人工修正。

---

# 注意事項

- AI 配對結果仍需人工確認。
- A 字幕建議使用已完成校對的字幕。
- B 字幕需具備正確或接近正確的時間軸。
- config.json 含有 API Key，請勿分享。

---

# 專案資料

程式會在同一資料夾建立：

```
config.json
autosave_project.json
ai_check_result.json
logs/
temp/
```

其中：

- config.json：API 設定
- autosave_project.json：自動儲存
- ai_check_result.json：AI 偵錯資訊
- logs：執行紀錄
- temp：暫存資料

---

# License

僅供學習與個人使用。
