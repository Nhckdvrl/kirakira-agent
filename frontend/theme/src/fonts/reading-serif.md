# 中英文阅读字体

共享阅读组合使用 Source Serif 4 英文与 Noto Serif SC 中文，保留 JetBrains Mono 技术字体。
字体均本地发布，不依赖 Google Fonts 或客户端已有字体。可变字重范围为 200–900；
英文同时包含正常体、真实斜体与 optical size 轴。

来源为 Google Fonts 官方仓库的 `ofl/sourceserif4` 和 `ofl/notoserifsc`，
许可证分别见同目录 `sourceserif4-OFL.txt` 和 `notoserifsc-OFL.txt`（SIL OFL 1.1）。

- https://github.com/google/fonts/tree/main/ofl/notoserifsc
- https://github.com/google/fonts/tree/main/ofl/sourceserif4

Noto Serif SC 按字频分层切片（Version 2.003，源 `NotoSerifSC[wght].ttf` SHA256
`050080d9255a86808f2945bffac582b31ef32bc36411ce29563b4961670c66f9`），浏览器按
`unicode-range` 只下载页面用到的分片：

- `core`：拉丁、通用标点、箭头、CJK 标点与假名、全角形式；
- `common-1`：GB2312 一级字（3755），整片；
- `common-2-NN`：GB2312 二级字（3008）按部首顺序每 500 字一片——"阈""渚"这类现代常见字在二级字里，一页常有几个，细切后只多下载几片；
- `han-NN`、`ext-a-NN`：其余基本区汉字与扩展 A，按码位每 800 字一片；
- `symbols`、`astral`：其余符号与兼容字形、增补平面。

构建直接消费这些已签入的字体与 `reading-serif.css`，不在启动或构建时重新生成。
字体请求由 CSS 的 `unicode-range` 与页面实际字符决定；具体加载量取决于页面内容与浏览器缓存。

旧的一次性分片程序已不在发行版中。需要更换字体资产时，重新核对源文件、许可证、分片 cmap 与 CSS
范围的完整性和互斥性，并在实际桌面、窄屏浏览器中检查中文、英文、斜体和代码显示。
