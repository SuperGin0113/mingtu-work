"""
Gradio UI: 构建 golden_dataset.jsonl（追加写，不支持编辑/删除）。

用法（热加载，推荐开发时使用，改文件即自动重启）:
    cd /Users/gin/Downloads/westlaw
    .venv/bin/gradio evaluation/gradio_app.py

普通启动:
    .venv/bin/python evaluation/gradio_app.py
"""
from __future__ import annotations

import json
from pathlib import Path

import gradio as gr

from adapters import call_retrieve, RETRIEVE_URL

DEFAULT_OUT = Path(__file__).parent / "datasets" / "golden_dataset.jsonl"


def do_retrieve(query: str, top_k: float, cos_weight: float, rerank: bool):
    if not query.strip():
        return [], gr.update(choices=[], value=[]), "请输入 query", "[]"
    try:
        hits = call_retrieve(
            query,
            cos_weight=float(cos_weight),
            top_k=int(top_k),
            rerank_enabled=bool(rerank),
        )
    except Exception as e:
        return [], gr.update(choices=[], value=[]), f"检索失败: {e}", "[]"

    choices = []
    tooltips = []
    for i, h in enumerate(hits):
        title = h["doc"].get("title", "(无 title)")
        content = h.get("content", "") or ""
        snippet = content.replace("\n", " ")
        label = f"[{i+1:02d}] {title}  ——  {snippet}"
        choices.append((label, i))
        tooltips.append(f"[{i+1:02d}] {title}\n\n{content}")

    return (
        hits,
        gr.update(choices=choices, value=[]),
        f"检索到 {len(hits)} 条",
        json.dumps(tooltips, ensure_ascii=False),
    )


APPLY_TOOLTIPS_JS = """
(data) => {
    setTimeout(() => {
        try {
            const arr = JSON.parse(data || "[]");
            const root = document.querySelector('#results-checkbox');
            if (!root) return;

            // 注入一次省略号样式：label 文字最多 2 行，超出 …
            if (!document.getElementById('__results_clamp_style')) {
                const st = document.createElement('style');
                st.id = '__results_clamp_style';
                st.textContent = `
                    #results-checkbox label { align-items: flex-start; }
                    #results-checkbox label span {
                        display: -webkit-box;
                        -webkit-line-clamp: 2;
                        -webkit-box-orient: vertical;
                        overflow: hidden;
                        text-overflow: ellipsis;
                        word-break: break-word;
                    }
                    #__custom_tip { scrollbar-gutter: stable; }
                    #__custom_tip::-webkit-scrollbar { width: 10px; height: 10px; }
                    #__custom_tip::-webkit-scrollbar-track {
                        background: rgba(0,0,0,0.06);
                        border-radius: 6px;
                    }
                    #__custom_tip::-webkit-scrollbar-thumb {
                        background: #b0b0b0;
                        border-radius: 6px;
                        border: 2px solid #fffdf5;
                    }
                    #__custom_tip::-webkit-scrollbar-thumb:hover { background: #888; }
                `;
                document.head.appendChild(st);
            }

            let tip = document.getElementById('__custom_tip');
            if (!tip) {
                tip = document.createElement('div');
                tip.id = '__custom_tip';
                Object.assign(tip.style, {
                    position: 'fixed', zIndex: 99999, maxWidth: '480px',
                    boxSizing: 'border-box',
                    overflow: 'auto',
                    padding: '12px 14px', background: '#fffdf5',
                    color: '#1a1a1a', fontSize: '13px', lineHeight: '1.55',
                    border: '1px solid #c8c8c8',
                    borderLeft: '4px solid #2563eb',
                    borderRadius: '6px',
                    boxShadow: '0 8px 28px rgba(0,0,0,0.55)',
                    whiteSpace: 'pre-wrap', pointerEvents: 'none',
                    display: 'none',
                });
                document.body.appendChild(tip);
            }

            // 共享状态（保证多次检索后 outside-click 监听只挂一份）
            const state = (window.__tipState = window.__tipState || {});
            state.pinned = null;

            // 始终允许鼠标交互，方便长内容滚动
            tip.style.pointerEvents = 'auto';
            const setPinnedStyle = (on) => {
                tip.style.borderLeftColor = on ? '#dc2626' : '#2563eb';
            };
            setPinnedStyle(false);

            const show = (lbl, text, pinned) => {
                tip.textContent = (pinned ? '📌 已固定（再次点击或点空白处取消）\\n\\n' : '') + text;
                tip.style.display = 'block';
                const pad = 8, margin = 8;
                const lr = lbl.getBoundingClientRect();
                // 先按内容自然高度测量
                tip.style.maxHeight = (window.innerHeight - margin * 2) + 'px';
                const r = tip.getBoundingClientRect();
                let x = lr.right + pad;
                if (x + r.width > window.innerWidth - 4) x = lr.left - r.width - pad;
                if (x < 4) x = 4;
                let y = lr.top;
                if (y + r.height > window.innerHeight - margin) y = window.innerHeight - r.height - margin;
                if (y < margin) y = margin;
                tip.style.left = x + 'px';
                tip.style.top  = y + 'px';
                // 收紧 maxHeight，确保从 y 到视口底刚好放得下
                tip.style.maxHeight = (window.innerHeight - y - margin) + 'px';
            };
            const hide = () => { tip.style.display = 'none'; };

            // hover 桥：离开 label 后延迟关闭，期间进入浮层可保持显示
            let hideTimer = null;
            const queueHide = () => {
                clearTimeout(hideTimer);
                hideTimer = setTimeout(() => { if (!state.pinned) hide(); }, 250);
            };
            tip.onmouseenter = () => clearTimeout(hideTimer);
            tip.onmouseleave = () => { if (!state.pinned) queueHide(); };

            const labels = root.querySelectorAll('label');
            labels.forEach((lbl, i) => {
                if (arr[i] === undefined) return;
                lbl.removeAttribute('title');
                lbl.style.cursor = 'help';

                lbl.onmouseenter = () => {
                    clearTimeout(hideTimer);
                    if (state.pinned) return;
                    show(lbl, arr[i], false);
                };
                lbl.onmouseleave = () => {
                    if (state.pinned) return;
                    queueHide();
                };

                // 点击「文字」固定/取消，并阻止 label 默认地切换 checkbox
                const span = lbl.querySelector('span');
                if (span) {
                    span.style.cursor = 'pointer';
                    span.onclick = (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        if (state.pinned === lbl) {
                            state.pinned = null;
                            setPinnedStyle(false);
                            hide();
                        } else {
                            state.pinned = lbl;
                            setPinnedStyle(true);
                            show(lbl, arr[i], true);
                        }
                    };
                }
            });

            // 点击外部取消固定
            if (state.docHandler) document.removeEventListener('click', state.docHandler, true);
            state.docHandler = (e) => {
                if (!state.pinned) return;
                if (root.contains(e.target) || tip.contains(e.target)) return;
                state.pinned = null;
                setPinnedStyle(false);
                hide();
            };
            document.addEventListener('click', state.docHandler, true);
        } catch(e) { console.error('tooltip error', e); }
    }, 80);
    return [];
}
"""


def do_save(query: str, hits: list, selected: list[int], category: str,
            reference_answer: str, out_path: str):
    if not query.strip():
        return "保存失败：query 为空", gr.update()
    if not hits:
        return "保存失败：请先检索", gr.update()
    if not selected:
        return "保存失败：未选中任何文档", gr.update()

    titles = list({hits[i]["doc"].get("title", "") for i in selected if 0 <= i < len(hits)})
    record = {
        "query": query.strip(),
        "relevant_titles": titles,
        "category": (category or "general").strip(),
    }
    if reference_answer.strip():
        record["reference_answer"] = reference_answer.strip()

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    total = count_records(out)
    msg = f"已保存第 {total} 条 → relevant_titles: {titles}"
    return msg, list_recent(str(out))


def count_records(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def list_recent(path: str, n: int = 5) -> str:
    p = Path(path)
    if not p.exists():
        return "_（暂无）_"
    with open(p, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    if not lines:
        return "_（暂无）_"
    out_lines = [f"**累计 {len(lines)} 条**，最近 {min(n, len(lines))} 条:"]
    for line in lines[-n:]:
        try:
            r = json.loads(line)
            q = r.get("query", "")[:60].replace("\n", " ")
            n_titles = len(r.get("relevant_titles", []))
            cat = r.get("category", "")
            out_lines.append(f"- `[{cat}]` {q} → {n_titles} 篇")
        except json.JSONDecodeError:
            out_lines.append(f"- {line[:80]}")
    return "\n".join(out_lines)


with gr.Blocks(title="Golden Dataset Builder") as demo:
    gr.Markdown(f"# Golden Dataset Builder\n检索服务: `{RETRIEVE_URL}`")

    hits_state = gr.State([])

    with gr.Row():
        with gr.Column(scale=2):
            query = gr.Textbox(label="Query", lines=5, placeholder="输入要测试的查询，可粘贴长文本")
            with gr.Row():
                top_k = gr.Number(label="top_k", value=20, precision=0)
                cos_weight = gr.Slider(label="cos_weight", minimum=0.0, maximum=1.0, step=0.05, value=1)
            with gr.Row():
                rerank = gr.Checkbox(label="rerank", value=True)
                category = gr.Textbox(label="category", value="general")
            retrieve_btn = gr.Button("检索", variant="primary")
            status = gr.Textbox(label="状态", interactive=False)

        with gr.Column(scale=3):
            results = gr.CheckboxGroup(
                label="检索结果（勾选相关文档，鼠标悬停查看全文）",
                choices=[],
                elem_id="results-checkbox",
            )
            tooltip_data = gr.Textbox(visible=False, value="[]")
            reference_answer = gr.Textbox(label="参考答案（可选）", lines=3)
            out_path = gr.Textbox(label="输出文件", value=str(DEFAULT_OUT))
            save_btn = gr.Button("保存", variant="primary")
            save_msg = gr.Textbox(label="保存结果", interactive=False)

    gr.Markdown("### 历史")
    recent_display = gr.Markdown(list_recent(str(DEFAULT_OUT)))
    refresh_btn = gr.Button("刷新历史")

    retrieve_btn.click(
        do_retrieve,
        inputs=[query, top_k, cos_weight, rerank],
        outputs=[hits_state, results, status, tooltip_data],
    ).then(
        None,
        inputs=[tooltip_data],
        outputs=[],
        js=APPLY_TOOLTIPS_JS,
    )
    save_btn.click(
        do_save,
        inputs=[query, hits_state, results, category, reference_answer, out_path],
        outputs=[save_msg, recent_display],
    )
    refresh_btn.click(
        lambda p: list_recent(p),
        inputs=[out_path],
        outputs=[recent_display],
    )


if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7860, share=False, inbrowser=False)
