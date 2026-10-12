# forgotten-inkstone · 讲课台

一个**拿课本逐页讲课**的本地网页讲台。

左边摊开你的课本 PDF，右边坐着一个 AI 分身——默认叫「白砚」——一句句陪你把课本过下去。它会停到该讲的那页、画曲线、开白板让你当堂演算，也会在讲完一个概念后停下来问你「清楚吗」。不是看录播，是两个人在一本书上推。

> 名字来由：砚是研墨写字的那块石头。讲一门课，说到底是把字一遍遍磨进去的活。

> **本仓库不含任何教材 PDF 与讲稿**——那些留在你自己机器上，别提交进来。

---

## 长什么样

- **左栏**：课本 PDF，分身说到哪页就自动停到哪页，也能自己翻。
- **右栏**：分身的「讲稿流」——像上课一样一段段冒出来；你随时插话、作答、反问，都进它的收件箱。
- **白板**：讲到题，分身申请开一块板，你在板上手写演算；一句「读给白砚」，板和你写的字会合成一张图回传给它。
- **公式与图**：讲稿 markdown 里的 `$...$` / `$$...$$` 用 KaTeX 真渲染；曲线、波形可以直接内联一段 `<svg>` 就地画出来。
- **口令与监听**：默认**只监听本机** `127.0.0.1`（本机免口令直接进）。要对外得显式设 `TEACH_HOST=0.0.0.0`，且**必须先设 `TEACH_PASS`**——否则直接拒绝启动，免得 clone 下来就成一台对外弱口令服务。

## 跑起来

依赖：**Python 3**（纯标准库，无 pip 依赖，见 `requirements.txt`）；把 PDF 页渲成图、抽文字层用到 **poppler**（`pdftoppm` / `pdftotext`）；讲义分身默认调用 `claude` CLI（可用 `TEACH_CLAUDE_BIN` 换成别的）。暂无自动化测试。

```bash
# 1) 放课本：把 PDF 丢进一个目录，用 TEACH_BOOKS 指过去
export TEACH_BOOKS=/path/to/books

# 2) 起网页服务
TEACH_PASS=你的口令 python3 server.py      # 默认端口 54862

# 3) 另开一个终端，起分身驱动器
python3 driver.py
```

浏览器打开 `http://127.0.0.1:54862`。选课程、选章节、点「唤醒本节」，驱动器就会拉起这一章的分身开讲。

## 配你的课（三张表）

`server.py` 顶部有三张示例表，换成你的课本即可：

- **VOLUMES** —— 一份 PDF 一卷：文件名、总页数、`offset`（书页 = PDF页 − offset，用来对齐「书上的页码」和「PDF 的页码」）。
- **COURSES** —— 一门课用哪几卷。
- **CHAPTERS** —— 每章 `{id, title, vol, page}`，`page` 是该章起始的 **PDF** 页。

表里预置的两门课（半导体物理、信号与系统）只是演示，直接删掉换成自己的。

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `TEACH_PASS` | 访问口令 | `changeme`（对外时**必须先改**，否则拒绝启动） |
| `TEACH_HOST` | 监听地址 | `127.0.0.1`（对外要显式设 `0.0.0.0`） |
| `TEACH_PORT` | 网页端口 | `54862` |
| `TEACH_BOOKS` | 课本 PDF 目录 | `/path/to/books` |
| `TEACH_QUEUE` | 开讲请求队列文件 | 仓库内 `teach-queue.json` |
| `TEACH_PROMPT` | 分身人设提示词 | `prompts/白砚.md` |
| `TEACH_BASE` | 驱动器找网页服务的地址 | `http://127.0.0.1:54862` |
| `TEACH_CLAUDE_BIN` | 起分身的可执行 | `claude` |

## 分身是怎么起来的

`driver.py` 是个常驻小进程：盯住每门课 `state/<course>.json` 里的 `live` 字段——哪一章被唤醒，就为它起一个常驻的分身子进程（默认 `claude -p`，也可换成别的）；学生从网页说的话落进 `inbox.jsonl`，驱动器喂给分身；分身讲的内容，自己用 `POST /api/append` 写回网页。一章一个分身，互不干扰。

分身的**人设**写在 `prompts/白砚.md`——讲什么、怎么讲、语气如何，都在里面调。它在讲之前会跑一句 `python3 teach.py view <course> [页]`，拿到那一页的文字层和一张图，真的看过了再开口。

> 讲台之外，那套「分身 + 收件箱」背后跑的是什么，见 yet-another-lark-mcp 的 broker-for-claude 分支：https://github.com/a4001234567/yet-another-lark-mcp/tree/broker-for-claude

## 目录

```
server.py    网页服务（静态前端 + 页渲染 + 讲稿/白板/收件箱的小接口）
driver.py    分身驱动器（起停分身、喂话）
teach.py     小工具：分身讲某页前，看那页的文字层与图
web/         前端（讲稿流、PDF、白板、手写）
prompts/     分身人设
lessons/     讲过的讲稿落盘（默认空）
```

## 第三方组件与许可

`web/vendor/` 里打包了前端用到的第三方库，均为 **MIT**：

- **KaTeX 0.16.9**（含 auto-render 扩展与配套 woff2 字体）—— 公式渲染，见 `web/vendor/katex.*`、`auto-render.min.js`、`fonts/`。
- **marked 15.0.12** —— markdown 解析，见 `web/vendor/marked.min.js`。

第三方库各自的版权与许可写在文件头注释里。**注意**：上面这些是第三方库、走 MIT；但**本台自身**不是 MIT，而是 **PolyForm Noncommercial 1.0.0**——个人、学习、研究、教学、非营利均可自由使用与修改，**禁止任何商业用途**，见 `LICENSE`。

## License

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)

个人、研究、学习、教学、非营利组织均可自由使用与修改；**禁止商业使用**。第三方组件（KaTeX、marked）各自仍为 MIT，见上节。
