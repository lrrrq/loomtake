from __future__ import annotations

import base64
from html import escape
import mimetypes
from pathlib import Path

from openpyxl import load_workbook


def write_visual_review_html_from_workbook(
    workbook_path: str | Path,
    html_path: str | Path | None = None,
) -> Path:
    workbook_path = Path(workbook_path)
    html_path = Path(html_path) if html_path else Path(str(workbook_path) + ".visual_review.html")
    wb = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        rows = _review_rows(wb)
    finally:
        wb.close()
    html_path.write_text(_render_html(workbook_path, rows), encoding="utf-8")
    return html_path


def _review_rows(wb) -> list[dict]:
    from .excel_io import read_managed_sheet_rows

    sheet_name = "配图复核" if "配图复核" in wb.sheetnames else "视觉资产"
    sheet = wb[sheet_name]
    rows = []
    for values in read_managed_sheet_rows(wb, sheet.title):
        row = {
            header: "" if value is None else str(value)
            for header, value in values.items()
            if header
        }
        if any(row.values()):
            rows.append(row)
    return rows


def _render_html(workbook_path: Path, rows: list[dict]) -> str:
    matched = sum(1 for row in rows if row.get("配图判断") == "匹配")
    reference = sum(1 for row in rows if row.get("配图判断") == "功能参考")
    failed = sum(1 for row in rows if row.get("配图判断") == "不匹配")
    unread = sum(1 for row in rows if "未读取" in row.get("视觉读取状态", ""))
    low_res = sum(1 for row in rows if "低清" in row.get("质量指标", ""))
    cards = "\n".join(_render_card(row) for row in rows)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>配图高清复核</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", Arial, sans-serif; color: #111; background: #f6f7f8; }}
    header {{ position: sticky; top: 0; z-index: 2; padding: 18px 22px; background: rgba(255,255,255,.96); border-bottom: 1px solid #ddd; }}
    h1 {{ margin: 0 0 8px; font-size: 22px; }}
    .meta {{ color: #555; font-size: 13px; word-break: break-all; }}
    .stats {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }}
    .stat {{ padding: 6px 10px; border: 1px solid #ddd; border-radius: 6px; background: #fff; font-size: 13px; }}
    main {{ padding: 18px; display: grid; grid-template-columns: repeat(auto-fit, minmax(520px, 1fr)); gap: 18px; }}
    article {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; overflow: hidden; }}
    .image-wrap {{ background: #111; display: flex; align-items: center; justify-content: center; min-height: 300px; }}
    img {{ display: block; max-width: 100%; height: auto; }}
    .content {{ padding: 14px 16px 16px; }}
    h2 {{ margin: 0 0 8px; font-size: 20px; }}
    .status {{ font-weight: 700; }}
    .ok {{ color: #087a2d; }}
    .fail {{ color: #bd1111; }}
    .weak {{ color: #9a6500; }}
    dl {{ display: grid; grid-template-columns: 96px 1fr; gap: 7px 12px; margin: 12px 0 0; font-size: 14px; line-height: 1.45; }}
    dt {{ color: #555; }}
    dd {{ margin: 0; white-space: pre-wrap; word-break: break-word; }}
    a {{ color: #0b63ce; }}
  </style>
</head>
<body>
  <header>
    <h1>配图高清复核</h1>
    <div class="meta">来源 workbook：{escape(workbook_path.name)}</div>
    <div class="stats">
      <div class="stat">镜头数：{len(rows)}</div>
      <div class="stat">匹配：{matched}</div>
      <div class="stat">功能参考：{reference}</div>
      <div class="stat">不匹配：{failed}</div>
      <div class="stat">未读取：{unread}</div>
      <div class="stat">低清：{low_res}</div>
    </div>
  </header>
  <main>
{cards}
  </main>
</body>
</html>
"""


def _render_card(row: dict) -> str:
    shot_no = row.get("镜号", "")
    status = row.get("配图判断", "") or "未判断"
    css = "ok" if status == "匹配" else "fail" if status == "不匹配" else "weak"
    path = row.get("原图路径", "")
    image_html = _image_html(path)
    return f"""    <article>
      <div class="image-wrap">{image_html}</div>
      <div class="content">
        <h2>镜 {escape(shot_no)} <span class="status {css}">{escape(status)}</span> 分：{escape(row.get("视觉匹配分", ""))}</h2>
        <dl>
          <dt>画面需求</dt><dd>{escape(row.get("画面描述", ""))}</dd>
          <dt>视觉摘要</dt><dd>{escape(row.get("视觉内容摘要", ""))}</dd>
          <dt>命中要素</dt><dd>{escape(row.get("命中视觉要素", ""))}</dd>
          <dt>缺失要素</dt><dd>{escape(row.get("缺失视觉要素", ""))}</dd>
          <dt>冲突要素</dt><dd>{escape(row.get("冲突视觉要素", ""))}</dd>
          <dt>读取状态</dt><dd>{escape(row.get("视觉读取状态", ""))}</dd>
          <dt>质量</dt><dd>{escape(row.get("质量指标", ""))}</dd>
          <dt>原图</dt><dd>{_path_link(path)}</dd>
        </dl>
      </div>
    </article>"""


def _image_html(path_text: str) -> str:
    path = Path(path_text)
    if not path.is_absolute() or not path.exists():
        return '<span style="color:#fff">无可显示图片</span>'
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f'<img src="data:{mime_type};base64,{encoded}" loading="lazy" alt="镜头参考图">'


def _path_link(path_text: str) -> str:
    path = Path(path_text)
    if not path.is_absolute() or not path.exists():
        return escape(path_text)
    return escape(path.name)
