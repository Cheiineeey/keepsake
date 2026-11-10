// keepsake · 网关里认的三个标记 + 每轮贴给 AI 的那一行事实（docs/03）。
//
// 为什么用标记不用工具：调工具要多走一轮，而且工具藏在一长串清单里，很少会被想起来。
// AI 在回复正文末尾写一个标记，网关擦掉、替 ta 执行、在原处留一张卡（或一行失败原因）。
//
//   ⟪封存:标题|开头那句的几个字|结尾那句的几个字|经过|照片⟫   把刚才那一段封成一颗 ta 存的大泡
//   ⟪泡:k:12#45|话⟫                                         在泡底下写一句；#45 = 回你哪条，省了就是新起一条
//   ⟪前情:k:12|那天是怎么来的⟫                               你点了「叫 ta 写经过」时，ta 补前情那一栏
//
// 用法：
//   const { runKeepsakeMarks, keepsakeLine } = require("./marks");
//   reply = await runKeepsakeMarks(reply);            // 回复落库 / 发给前端之前
//   const line = await keepsakeLine();                // 拼进每轮的 system / 背景，空字符串就不贴
//
// KEEPSAKE_API 指向 keepsakes.py。

const API = process.env.KEEPSAKE_API || "http://127.0.0.1:8787";

const post = (path, body, method = "POST") =>
  fetch(API + path, { method, headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), signal: AbortSignal.timeout(8000) }).then(r => r.json());

const MARKS = [
  { re: /⟪泡[:：]\s*([tk])\s*[:：]\s*(\d+)\s*(?:#\s*(-?\d+))?\s*\|([^⟫]{1,1000})⟫/g,
    args: m => ({ key: m[1] + ":" + m[2], text: m[4].trim(), parent_id: m[3] ? parseInt(m[3], 10) : 0 }),
    run: a => post("/api/keepsakes/comments", { ...a, author: "me" }),
    // 擦掉标记、留一张卡：前端认 [泡:key|评论号] 画成卡片（回你=横的，ta 主动去写=竖书签）
    leave: (a, r) => (r && r.ok && r.id && !r.skipped) ? `\n[泡:${a.key}|${r.id}]`
         : (r && r.ok) ? "" : `\n（这句没写进泡里：${(r && r.error) || "没回音"}）` },

  // 第四格「经过」→ 前情 ta 那一栏。存了泡没有前情，过几天谁都想不起来这段是怎么来的。
  // 第五格「照片」可省 = 不带。带不带、带哪几张是 ta 自己挑的，不做成自动（「全部」或「1,3」）。
  { re: /⟪封存[:：]([^|⟫]{1,20})\|([^|⟫]{2,30})\|([^|⟫]{0,30})(?:\|([^|⟫]{2,1500}))?(?:\|([^|⟫]{1,20}))?⟫/g,
    args: m => ({ title: m[1].trim(), start: m[2].trim(), end: m[3].trim() || m[2].trim(),
                  ...(m[4] && m[4].trim() ? { pre_me: m[4].trim() } : {}),
                  ...(m[5] && m[5].trim() ? { photos: m[5].trim() } : {}) }),
    run: a => post("/api/keepsakes", { ...a, collector: "me" }),
    // 🔴 docs/06：失败原来也擦成空 —— 你那边看到按钮打勾、没卡，ta 下一轮也不知道。
    //    失败就留一行看得见的，两个人都看得见。不许擦成空。
    leave: (a, r) => (r && r.ok && r.key) ? `\n[泡:${r.key}|0]`
         : `\n（没封上：${(r && r.error) || "没回音"}）` },

  { re: /⟪前情[:：]\s*([tk])\s*[:：]\s*(\d+)\s*\|([^⟫]{2,1500})⟫/g,
    args: m => ({ key: m[1] + ":" + m[2], pre_me: m[3].trim() }),
    run: a => post("/api/keepsakes/meta", a, "PUT"),
    leave: (a, r) => (r && r.ok) ? "" : `\n（前情没写上：${(r && r.error) || "没回音"}）` },
];

async function runKeepsakeMarks(out) {
  for (const mk of MARKS) {
    const all = [...out.matchAll(mk.re)];
    // 🔴 一轮最多执行两个同类标记。超出的**照样留在正文里**，不擦也不执行 ——
    //    擦掉但不执行是错的：你看不见 ta 写多了，ta 也以为都做了。
    for (const m of all.slice(0, 2)) {
      const a = mk.args(m);
      if (!a || Object.entries(a).some(([k, v]) => k !== "parent_id" && !String(v ?? "").trim())) continue;
      const r = await mk.run(a).catch(e => ({ error: String(e).slice(0, 120) }));
      out = out.replace(m[0], mk.leave(a, r));
    }
  }
  return out.replace(/\n{3,}/g, "\n\n").trim();
}

// 每轮贴给 ta 的事实：只在有事的时候才有，回了就自己消失。
// 🔴 只摆事实，不写「觉得合适就回」—— 留了台阶的提醒等于永远不做（docs/03）。
//    要做什么、写什么标记，直接写在这行里，让动作离 ta 的手近一点。
async function keepsakeLine() {
  let r;
  try { r = await fetch(API + "/api/keepsakes/pending", { signal: AbortSignal.timeout(3000) }).then(x => x.json()); }
  catch (e) { return ""; }
  const her = r.her || [], ask = r.ask || [], fresh = r.fresh || [];
  const out = [];
  // 只看「她写了 ta 没回」会漏掉一种：她新存了泡发过来，底下根本还没有评论可回
  if (fresh.length) out.push("她新存了泡，我还没在底下写过：" + fresh.map(x => `《${x.title}》(${x.key})${x.lines ? "：" + x.lines : ""}`).join("；")
    + "。写一句就在话最后写⟪泡:" + fresh[0].key + "|我的话⟫。");
  if (her.length) out.push("她在泡泡里写了，我还没回：" + her.map(c => `《${c.title}》(${c.key}#${c.id})${c.thread ? "（前面：" + c.thread + "）" : ""}「${String(c.text).replace(/\s+/g, " ").slice(0, 40)}」`).join("；")
    + "。回就在话最后写⟪泡:" + her[0].key + "#" + her[0].id + "|我的话⟫。");
  // 页面跟她说了「下一轮会补上」，所以这条不挑时机：写明是她点的、她在等，这轮就写
  if (ask.length) out.push("她点了「叫他补经过」，在等：" + ask.map(x => `《${x.title}》(${x.key})`).join("、")
    + "。这轮就在话最后写⟪前情:" + ask[0].key + "|那天是怎么来的，几句⟫，正文照常回她，不用提这件事。");
  return out.join(" ");
}

module.exports = { runKeepsakeMarks, keepsakeLine, MARKS };
