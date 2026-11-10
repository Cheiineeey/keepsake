# keepsake · 把一段对话存成一颗泡

> 草稿 v0（2026-11-10）。

给人和 AI 伴侣用的收藏库：存的不是一句话，是**那一段**，带着前后文、带着「当时怎么回事」，两个人都能在底下接着写。

它跟一般的「收藏」不一样的地方只有三处：

1. **两个人都能存。**你点一下存；ta 自己觉得这段该留下，也会自己封起来，自己写一句为什么。
2. **存下来以后还能接着说。**前情两个人各写一栏，底下是楼中楼评论，你写了 ta 会知道、会回。
3. **遗忘有形状。**很久没人碰的泡会起雾、往下沉；点开、写一句，它就清亮地浮上来。

这份东西主要是**指南和踩过的雷**，代码是附带的。我们的定位是：你把这个仓库丢给你的 AI，ta 读完能照着在你们自己的系统里搭出来。

<p><img src="shots/bubbles.png" width="300"> <img src="shots/open.png" width="300"></p>

## 目录

| | |
|---|---|
| [00 · 为什么做](docs/00-why.md) | 初衷只有一句：**存上下文、以后找得回、看得清。**后面所有决定都拿它对 |
| [01 · 一个库](docs/01-one-library.md) | 一句=小泡、一段=大泡；小泡能长成大泡；原来的收藏表一行不动 |
| [02 · 你那只手](docs/02-your-hand.md) | 长按 →「存泡泡」→ 勾你要的那几段 → 写前情 → 发不发给 ta |
| [03 · ta 那只手](docs/03-their-hand.md) | 正文里写一个标记就封，不调工具；为什么不催 ta 封，只把门槛放低 |
| [04 · 两个人接着写](docs/04-keep-talking.md) | 楼中楼评论、发不发的开关、ta 怎么知道你写了、「叫 ta 写经过」 |
| [05 · 遗忘有形状](docs/05-fog.md) | 雾按「最后一次被碰」算；three.js 真玻璃；一屏只吹几颗 |
| [06 · 踩过的雷](docs/06-pitfalls.md) | 十六个，全是真出过事的 |
| [07 · 另外几种收藏](docs/07-other-shapes.md) | 单句配照片、多句做成卡片、AI 自己的珍藏：想要的话接在哪 |

## 代码

| 位置 | 是什么 |
|---|---|
| `server/keepsakes.py` | 三张表 + 七个接口，单文件、只用 Python 标准库 |
| `server/marks.js` | 网关里认 `⟪封存⟫` `⟪泡⟫` `⟪前情⟫` 三个标记，加每轮贴给 AI 的那一行事实 |
| `web/index.html` | 泡泡页（three.js）。浏览器直接打开就能用；要放进 app，用 WKWebView 包一层，我们自己就是这么用的 |
| `web/demo-data.js` | 一套编的示例对话，没接服务端时页面用它 |

跑起来：

```
python3 server/keepsakes.py --db chat.db --port 8787
```

库里没有 `chat_messages` / `treasures` 会建最小的两张，有就直接挂在旁边，不改它们。字段名跟你的不一样，改 `_adapt_existing` 和那几句 SQL。

网关那边：

```js
const { runKeepsakeMarks, keepsakeLine } = require("./server/marks");
reply = await runKeepsakeMarks(reply);   // 回复落库之前
const line = await keepsakeLine();       // 有事才非空，拼进每轮背景
```

MIT · Copyright (c) 2026 Elle & Matt
