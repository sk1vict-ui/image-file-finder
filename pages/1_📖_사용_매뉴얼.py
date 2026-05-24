"""사용 매뉴얼 페이지 — MANUAL.md를 그대로 렌더링."""
from pathlib import Path
import streamlit as st

st.set_page_config(
    page_title="사용 매뉴얼 - 캡쳐 이미지 원본 파일 찾기",
    page_icon="📖",
    layout="wide",
)

MANUAL_PATH = Path(__file__).parent.parent / "MANUAL.md"

st.sidebar.success("📖 사용 매뉴얼을 보고 계십니다")
st.sidebar.page_link("app.py", label="← 앱으로 돌아가기", icon="🏠")

if not MANUAL_PATH.exists():
    st.error(f"매뉴얼 파일을 찾을 수 없습니다: {MANUAL_PATH}")
    st.stop()

manual_text = MANUAL_PATH.read_text(encoding="utf-8")

col_dl1, col_dl2, _ = st.columns([1, 1, 4])
with col_dl1:
    st.download_button(
        label="📥 Markdown 다운로드",
        data=manual_text,
        file_name="MANUAL.md",
        mime="text/markdown",
        use_container_width=True,
    )
with col_dl2:
    html_export = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>캡쳐 이미지 원본 파일 찾기 - 사용 매뉴얼</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans KR", sans-serif;
         max-width: 860px; margin: 2em auto; padding: 0 1em; line-height: 1.7; color: #222; }}
  h1, h2, h3 {{ border-bottom: 1px solid #eee; padding-bottom: 0.3em; }}
  code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; }}
  pre {{ background: #f4f4f4; padding: 1em; border-radius: 5px; overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f8f8f8; }}
  blockquote {{ border-left: 4px solid #ddd; padding-left: 1em; color: #666; margin-left: 0; }}
</style>
</head>
<body>
<div id="content"></div>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
  document.getElementById('content').innerHTML = marked.parse({manual_text!r});
</script>
</body>
</html>"""
    st.download_button(
        label="📥 HTML 다운로드",
        data=html_export,
        file_name="MANUAL.html",
        mime="text/html",
        use_container_width=True,
    )

st.divider()
st.markdown(manual_text)
