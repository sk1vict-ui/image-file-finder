import io
import os
import shutil
import subprocess
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

import cv2
import fitz  # PyMuPDF
import imagehash
import numpy as np
import streamlit as st
from PIL import Image

st.set_page_config(
    page_title="캡쳐 이미지 원본 파일 찾기",
    layout="wide",
)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
PDF_EXTS = {".pdf"}
OFFICE_EXTS = {".docx", ".doc", ".pptx", ".ppt"}
SUPPORTED_CANDIDATE_EXTS = IMAGE_EXTS | PDF_EXTS | OFFICE_EXTS

ZOOM_OPTIONS = {"보통 (zoom 2.0)": 2.0, "고해상도 (zoom 3.0)": 3.0}
PHASH_HASH_SIZE = 8  # 64-bit hash
TEMPLATE_SCALES = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
MAX_PREVIEW_WIDTH = 1200
MAX_MATCH_SIZE = 1600


# --------------------------------------------------------------------------------------
# 파일 / 이미지 유틸
# --------------------------------------------------------------------------------------

def save_uploaded_file(uploaded_file, temp_dir: str) -> str:
    """업로드된 파일을 임시 폴더에 저장하고 경로 반환."""
    safe_name = Path(uploaded_file.name).name
    out_path = os.path.join(temp_dir, safe_name)
    base, ext = os.path.splitext(out_path)
    i = 1
    while os.path.exists(out_path):
        out_path = f"{base}_{i}{ext}"
        i += 1
    with open(out_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return out_path


def load_capture_image(uploaded_file) -> Image.Image:
    """업로드된 캡쳐 이미지를 PIL Image(RGB)로 변환."""
    data = uploaded_file.getbuffer()
    img = Image.open(io.BytesIO(bytes(data)))
    return img.convert("RGB")


def cv2_to_pil(image: np.ndarray) -> Image.Image:
    if image.ndim == 2:
        return Image.fromarray(image)
    return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


def pil_to_cv2(image: Image.Image) -> np.ndarray:
    arr = np.array(image.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def resize_max(image: np.ndarray, max_size: int = MAX_MATCH_SIZE) -> Tuple[np.ndarray, float]:
    """가로/세로 중 최댓값이 max_size를 넘으면 비율 유지 리사이즈. (img, scale) 반환."""
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= max_size:
        return image, 1.0
    scale = max_size / longest
    new_w, new_h = int(w * scale), int(h * scale)
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return resized, scale


def preprocess_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    try:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
    except Exception:
        pass
    return gray


# --------------------------------------------------------------------------------------
# 점수 계산
# --------------------------------------------------------------------------------------

def compute_phash_score(capture_pil: Image.Image, candidate_pil: Image.Image) -> float:
    try:
        h1 = imagehash.phash(capture_pil, hash_size=PHASH_HASH_SIZE)
        h2 = imagehash.phash(candidate_pil, hash_size=PHASH_HASH_SIZE)
        distance = h1 - h2
        score = max(0.0, 100.0 - distance * 100.0 / 32.0)
        return float(min(100.0, score))
    except Exception:
        return 0.0


def compute_template_match_score(
    capture_cv: np.ndarray, candidate_cv: np.ndarray
) -> Tuple[float, Optional[Tuple[int, int, int, int]]]:
    """다양한 스케일로 템플릿 매칭 후 (점수 0~100, bbox in candidate coords).

    저분산(거의 단색) 캡쳐는 의미없는 100점 매칭을 발생시키므로 점수를 감쇠시킨다.
    """
    try:
        cap_gray = preprocess_gray(capture_cv)
        cand_gray = preprocess_gray(candidate_cv)
        cand_h, cand_w = cand_gray.shape[:2]

        cap_std = float(cap_gray.std())
        # stddev<8: 거의 단색. 8~20 사이는 약하게 감쇠.
        if cap_std < 8.0:
            confidence = 0.0
        elif cap_std < 20.0:
            confidence = (cap_std - 8.0) / 12.0
        else:
            confidence = 1.0

        best_score = 0.0
        best_bbox: Optional[Tuple[int, int, int, int]] = None

        for scale in TEMPLATE_SCALES:
            tw = int(cap_gray.shape[1] * scale)
            th = int(cap_gray.shape[0] * scale)
            if tw < 16 or th < 16:
                continue
            if tw > cand_w or th > cand_h:
                continue
            template = cv2.resize(cap_gray, (tw, th), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(cand_gray, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            # 매칭 영역의 분산도 함께 확인 — 후보 영역이 거의 단색이면 의미없는 매칭
            x1, y1 = max_loc
            patch = cand_gray[y1:y1 + th, x1:x1 + tw]
            patch_std = float(patch.std()) if patch.size else 0.0
            local_conf = confidence
            if patch_std < 8.0:
                local_conf *= 0.0
            elif patch_std < 20.0:
                local_conf *= (patch_std - 8.0) / 12.0

            adjusted = max(0.0, float(max_val)) * local_conf
            if adjusted > best_score:
                best_score = adjusted
                best_bbox = (x1, y1, x1 + tw, y1 + th)

        score = max(0.0, min(100.0, best_score * 100.0))
        if score < 30.0:
            best_bbox = None
        return score, best_bbox
    except Exception:
        return 0.0, None


def compute_orb_match_score(
    capture_cv: np.ndarray, candidate_cv: np.ndarray
) -> Tuple[float, Optional[Tuple[int, int, int, int]], bool]:
    """ORB 특징점 매칭. (점수 0~100, bbox, homography_success)."""
    try:
        cap_gray = preprocess_gray(capture_cv)
        cand_gray = preprocess_gray(candidate_cv)

        orb = cv2.ORB_create(nfeatures=2000)
        kp1, des1 = orb.detectAndCompute(cap_gray, None)
        kp2, des2 = orb.detectAndCompute(cand_gray, None)

        if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
            return 0.0, None, False

        bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        try:
            knn_matches = bf.knnMatch(des1, des2, k=2)
        except cv2.error:
            return 0.0, None, False

        good = []
        for m_n in knn_matches:
            if len(m_n) < 2:
                continue
            m, n = m_n
            if m.distance < 0.75 * n.distance:
                good.append(m)

        good_count = len(good)
        if good_count < 4:
            score = min(40.0, good_count * 5.0)
            return float(score), None, False

        src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

        H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
        homography_success = False
        bbox: Optional[Tuple[int, int, int, int]] = None
        inlier_ratio = 0.0

        if H is not None and mask is not None:
            inliers = int(mask.sum())
            inlier_ratio = inliers / max(1, len(mask))
            if inliers >= 8 and inlier_ratio >= 0.25:
                homography_success = True
                h, w = cap_gray.shape[:2]
                corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
                projected = cv2.perspectiveTransform(corners, H).reshape(-1, 2)
                x_min = int(max(0, projected[:, 0].min()))
                y_min = int(max(0, projected[:, 1].min()))
                x_max = int(min(cand_gray.shape[1], projected[:, 0].max()))
                y_max = int(min(cand_gray.shape[0], projected[:, 1].max()))
                if x_max > x_min and y_max > y_min:
                    bbox = (x_min, y_min, x_max, y_max)
                else:
                    homography_success = False

        if good_count < 8:
            score = min(40.0, good_count * 5.0)
        else:
            score = min(100.0, 40.0 + good_count * 2.0 + inlier_ratio * 40.0)
            if homography_success:
                score = min(100.0, score + 5.0)

        return float(score), bbox, homography_success
    except Exception:
        return 0.0, None, False


def classify_score(score: float) -> str:
    if score >= 85:
        return "매우 유사"
    if score >= 65:
        return "유사"
    return "낮은 유사도"


# --------------------------------------------------------------------------------------
# 미리보기
# --------------------------------------------------------------------------------------

def make_preview_image(image_cv: np.ndarray, max_width: int = MAX_PREVIEW_WIDTH) -> Image.Image:
    h, w = image_cv.shape[:2]
    if w > max_width:
        ratio = max_width / w
        image_cv = cv2.resize(image_cv, (max_width, int(h * ratio)), interpolation=cv2.INTER_AREA)
    return cv2_to_pil(image_cv)


def draw_bounding_box(
    image_cv: np.ndarray, bbox: Optional[Tuple[int, int, int, int]]
) -> np.ndarray:
    if bbox is None:
        return image_cv.copy()
    out = image_cv.copy()
    x1, y1, x2, y2 = bbox
    h, w = out.shape[:2]
    x1 = max(0, min(w - 1, int(x1)))
    y1 = max(0, min(h - 1, int(y1)))
    x2 = max(0, min(w, int(x2)))
    y2 = max(0, min(h, int(y2)))
    if x2 <= x1 or y2 <= y1:
        return out
    thickness = max(2, int(round(max(h, w) / 400)))
    cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255), thickness)
    return out


# --------------------------------------------------------------------------------------
# PDF / Office 처리
# --------------------------------------------------------------------------------------

def render_pdf_pages(pdf_path: str, zoom: float):
    """제너레이터로 (page_number, pil_image) 반환."""
    doc = fitz.open(pdf_path)
    try:
        mat = fitz.Matrix(zoom, zoom)
        for i in range(doc.page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            yield (i + 1, img)
    finally:
        doc.close()


def find_libreoffice() -> Optional[str]:
    """LibreOffice 실행 경로 찾기. 없으면 None."""
    candidate = shutil.which("libreoffice") or shutil.which("soffice")
    if candidate:
        return candidate
    # Windows 기본 설치 경로
    for guess in [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]:
        if os.path.exists(guess):
            return guess
    return None


def convert_office_to_pdf(input_path: str, output_dir: str, timeout: int = 180) -> str:
    soffice = find_libreoffice()
    if not soffice:
        raise RuntimeError("LibreOffice가 설치되어 있지 않습니다.")

    os.makedirs(output_dir, exist_ok=True)
    cmd = [
        soffice,
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--convert-to",
        "pdf",
        "--outdir",
        output_dir,
        input_path,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"LibreOffice 변환 실패: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    base = Path(input_path).stem
    pdf_path = os.path.join(output_dir, base + ".pdf")
    if not os.path.exists(pdf_path):
        candidates = list(Path(output_dir).glob("*.pdf"))
        if not candidates:
            raise RuntimeError("변환된 PDF를 찾을 수 없습니다.")
        pdf_path = str(candidates[0])
    return pdf_path


# --------------------------------------------------------------------------------------
# 후보 비교
# --------------------------------------------------------------------------------------

def compare_candidate_image(
    capture_data: Dict[str, Any],
    candidate_pil: Image.Image,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    capture_cv = capture_data["cv"]
    capture_pil = capture_data["pil"]

    candidate_cv = pil_to_cv2(candidate_pil)
    candidate_cv_resized, _ = resize_max(candidate_cv, MAX_MATCH_SIZE)
    capture_cv_resized, _ = resize_max(capture_cv, MAX_MATCH_SIZE)

    phash_score = compute_phash_score(capture_pil, candidate_pil)
    template_score, template_bbox = compute_template_match_score(
        capture_cv_resized, candidate_cv_resized
    )
    orb_score, orb_bbox, hom_ok = compute_orb_match_score(
        capture_cv_resized, candidate_cv_resized
    )

    scores = {
        "ORB 특징점 매칭": orb_score,
        "템플릿 매칭": template_score,
        "전체 이미지 해시 비교": phash_score,
    }
    best_method = max(scores, key=lambda k: scores[k])
    final_score = max(orb_score, template_score, phash_score)
    final_score = max(0.0, min(100.0, final_score))

    bbox = None
    if best_method == "ORB 특징점 매칭" and orb_bbox is not None:
        bbox = orb_bbox
    elif best_method == "템플릿 매칭" and template_bbox is not None:
        bbox = template_bbox
    elif orb_bbox is not None:
        bbox = orb_bbox
    elif template_bbox is not None:
        bbox = template_bbox

    if bbox is not None:
        h, w = candidate_cv_resized.shape[:2]
        orig_h, orig_w = candidate_cv.shape[:2]
        scale_x = orig_w / w
        scale_y = orig_h / h
        bbox = (
            int(bbox[0] * scale_x),
            int(bbox[1] * scale_y),
            int(bbox[2] * scale_x),
            int(bbox[3] * scale_y),
        )

    preview = make_preview_image(candidate_cv)
    boxed_cv = draw_bounding_box(candidate_cv, bbox) if bbox is not None else candidate_cv
    boxed_preview = make_preview_image(boxed_cv) if bbox is not None else None

    result = {
        "file_name": metadata.get("file_name"),
        "file_type": metadata.get("file_type"),
        "page_number": metadata.get("page_number"),
        "image_number": metadata.get("image_number"),
        "score": final_score,
        "orb_score": orb_score,
        "template_score": template_score,
        "phash_score": phash_score,
        "method": best_method,
        "judgement": classify_score(final_score),
        "preview_image": preview,
        "boxed_preview_image": boxed_preview,
        "bbox": bbox,
        "homography_success": hom_ok,
    }
    return result


def process_pdf_file(
    file_path: str,
    original_name: str,
    capture_data: Dict[str, Any],
    zoom: float,
    file_type_label: Optional[str] = None,
    progress_text: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    label = file_type_label or "PDF"
    for page_no, page_img in render_pdf_pages(file_path, zoom):
        if progress_text is not None:
            progress_text.write(f"`{original_name}` — {page_no}페이지 처리 중...")
        try:
            metadata = {
                "file_name": original_name,
                "file_type": label,
                "page_number": page_no,
                "image_number": None,
            }
            results.append(compare_candidate_image(capture_data, page_img, metadata))
        except Exception:
            continue
    return results


def extract_embedded_images_from_ooxml(file_path: str) -> List[Tuple[str, Image.Image]]:
    """
    .pptx/.docx 파일은 ZIP 컨테이너이며 내부에 ppt/media/ 또는 word/media/
    경로로 이미지를 포함합니다. 외부 변환 도구(LibreOffice) 없이 이 이미지들을
    추출합니다. 반환: [(media_name, PIL.Image), ...]
    """
    results: List[Tuple[str, Image.Image]] = []
    image_exts_in_ooxml = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
    try:
        with zipfile.ZipFile(file_path) as zf:
            media_entries = [
                n for n in zf.namelist()
                if ("/media/" in n.lower())
                and Path(n).suffix.lower() in image_exts_in_ooxml
            ]
            media_entries.sort()
            for name in media_entries:
                try:
                    data = zf.read(name)
                    img = Image.open(io.BytesIO(data)).convert("RGB")
                    results.append((Path(name).name, img))
                except Exception:
                    continue
    except (zipfile.BadZipFile, KeyError):
        pass
    return results


def process_office_file(
    file_path: str,
    original_name: str,
    capture_data: Dict[str, Any],
    zoom: float,
    progress_text: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    ext = Path(original_name).suffix.lower().lstrip(".")
    label = ext.upper() if ext else "OFFICE"
    soffice_available = find_libreoffice() is not None
    is_ooxml = ext in {"docx", "pptx"}  # ZIP 기반 신규 포맷

    # 1순위: LibreOffice가 있으면 전체 페이지를 PDF로 변환해서 처리 (가장 정확)
    if soffice_available:
        try:
            with tempfile.TemporaryDirectory() as out_dir:
                if progress_text is not None:
                    progress_text.write(f"`{original_name}` — PDF 변환 중...")
                pdf_path = convert_office_to_pdf(file_path, out_dir)
                return process_pdf_file(
                    pdf_path, original_name, capture_data, zoom,
                    file_type_label=label, progress_text=progress_text,
                )
        except Exception as e:
            if not is_ooxml:
                raise  # .ppt/.doc은 폴백 불가
            # OOXML이면 폴백으로 진행
            if progress_text is not None:
                progress_text.write(
                    f"`{original_name}` — LibreOffice 변환 실패, 내장 이미지 추출 모드로 전환..."
                )

    # 2순위 (포터블/폴백): OOXML이면 내장 이미지 추출 후 각 이미지와 비교
    if is_ooxml:
        if progress_text is not None:
            progress_text.write(f"`{original_name}` — 내장 이미지 추출 중...")
        embedded = extract_embedded_images_from_ooxml(file_path)
        if not embedded:
            return []
        results: List[Dict[str, Any]] = []
        for idx, (media_name, img) in enumerate(embedded, start=1):
            if progress_text is not None:
                progress_text.write(
                    f"`{original_name}` — 내장 이미지 {idx}/{len(embedded)} 비교 중..."
                )
            try:
                metadata = {
                    "file_name": original_name,
                    "file_type": f"{label} (내장 이미지)",
                    "page_number": None,
                    "image_number": idx,
                }
                results.append(compare_candidate_image(capture_data, img, metadata))
            except Exception:
                continue
        return results

    # 3순위: .ppt/.doc 레거시 포맷인데 LibreOffice 없음
    raise RuntimeError(
        f"{label} 파일을 처리하려면 LibreOffice가 필요합니다. "
        ".pptx / .docx 형식으로 저장 후 다시 시도해주세요."
    )


def process_image_file(
    file_path: str,
    original_name: str,
    capture_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    ext = Path(original_name).suffix.lower().lstrip(".")
    label = ext.upper() if ext else "IMAGE"
    img = Image.open(file_path).convert("RGB")
    metadata = {
        "file_name": original_name,
        "file_type": label,
        "page_number": None,
        "image_number": 1,
    }
    return [compare_candidate_image(capture_data, img, metadata)]


# --------------------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------------------

def format_size(num_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def render_result_card(rank: int, result: Dict[str, Any]) -> None:
    with st.container(border=True):
        head_cols = st.columns([1, 4, 2, 2])
        head_cols[0].markdown(f"### #{rank}")
        head_cols[1].markdown(f"**{result['file_name']}**")
        head_cols[2].markdown(f"형식: `{result['file_type']}`")
        loc_parts = []
        if result.get("page_number"):
            loc_parts.append(f"{result['page_number']}페이지")
        if result.get("image_number"):
            loc_parts.append(f"이미지 {result['image_number']}")
        head_cols[3].markdown("위치: " + (", ".join(loc_parts) if loc_parts else "-"))

        score_cols = st.columns(4)
        score_cols[0].metric("최종 점수", f"{result['score']:.1f}", result["judgement"])
        score_cols[1].metric("ORB", f"{result['orb_score']:.1f}")
        score_cols[2].metric("템플릿", f"{result['template_score']:.1f}")
        score_cols[3].metric("pHash", f"{result['phash_score']:.1f}")

        st.caption(f"매칭 방식: {result['method']}")

        img_cols = st.columns(2)
        with img_cols[0]:
            st.markdown("**원본 미리보기**")
            st.image(result["preview_image"], use_container_width=True)
        with img_cols[1]:
            st.markdown("**매칭 영역**")
            if result.get("boxed_preview_image") is not None:
                st.image(result["boxed_preview_image"], use_container_width=True)
            else:
                st.info("매칭 영역을 특정하지 못했습니다.")


def main() -> None:
    st.title("캡쳐 이미지 원본 파일 찾기")
    st.write(
        "캡쳐 이미지를 업로드한 뒤, 원본 후보 파일을 여러 개 업로드하면 "
        "가장 유사한 원본 파일과 페이지를 찾아줍니다."
    )

    # 환경 상태 배너 (포터블 모드 안내)
    soffice_path = find_libreoffice()
    if soffice_path:
        st.caption(
            f"🟢 **전체 모드** — LibreOffice 감지됨 (`{soffice_path}`). "
            "PDF / 이미지 / PPTX / DOCX / PPT / DOC 모두 처리 가능."
        )
    else:
        st.caption(
            "🟡 **포터블 모드** — LibreOffice 없이 작동 중. "
            "**PDF / 이미지 / PPTX / DOCX** 처리 가능. "
            "_.ppt / .doc 레거시 파일은 처리 불가 — .pptx / .docx로 저장 후 재시도하거나 "
            "LibreOffice를 추가 설치해주세요._"
        )

    st.divider()
    st.header("1단계: 찾고 싶은 캡쳐 이미지 업로드")
    capture_file = st.file_uploader(
        "캡쳐 이미지 파일을 선택하세요 (PNG, JPG, JPEG, WEBP)",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=False,
        key="capture",
    )
    if capture_file is not None:
        try:
            preview = load_capture_image(capture_file)
            st.image(preview, caption="업로드된 캡쳐 이미지", width=360)
        except Exception as e:
            st.error(f"캡쳐 이미지를 열 수 없습니다: {e}")

    st.divider()
    st.header("2단계: 원본 후보 파일 추가")
    st.caption(
        "내 컴퓨터에서 검색 대상 파일들을 추가하세요. "
        "**파일 추가** — 한 번에 여러 개를 Ctrl+클릭(Mac은 ⌘) 또는 Shift+클릭으로 다중 선택할 수 있습니다. "
        "**폴더 통째로 추가** — 폴더 안의 모든 파일(하위 폴더 포함)을 한 번에 업로드합니다 "
        "(Chrome/Edge/Safari에서 지원). 같은 파일을 두 번 추가해도 자동으로 중복 제거됩니다."
    )

    if "uploaded_candidates" not in st.session_state:
        # name -> {"name": str, "bytes": bytes, "size": int, "relpath": str}
        st.session_state.uploaded_candidates = {}

    tab_files, tab_folder, tab_zip, tab_path = st.tabs(
        [
            "📄 파일로 추가",
            "📁 폴더 통째로 추가",
            "🗜️ ZIP으로 추가",
            "📂 경로 지정 (로컬 실행 시)",
        ]
    )

    with tab_files:
        st.caption(
            "파일 선택 창에서 **Ctrl+A** (Mac ⌘+A) 전체 선택, "
            "**Shift+클릭** 범위 선택, **Ctrl/⌘+클릭** 개별 선택 가능"
        )
        new_files = st.file_uploader(
            "파일 선택 (PDF, DOCX, DOC, PPTX, PPT, PNG, JPG, JPEG, WEBP)",
            type=["pdf", "docx", "doc", "pptx", "ppt", "png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            key="files_uploader",
        )
        if new_files:
            added = 0
            for f in new_files:
                key = f"file::{f.name}"
                if key not in st.session_state.uploaded_candidates:
                    st.session_state.uploaded_candidates[key] = {
                        "name": f.name,
                        "bytes": bytes(f.getbuffer()),
                        "size": f.size,
                        "relpath": f.name,
                    }
                    added += 1
            if added:
                st.success(f"{added}개 파일이 추가되었습니다.")

    with tab_folder:
        st.caption(
            "📁 버튼을 누르면 OS의 **폴더 선택** 창이 열립니다. "
            "선택한 폴더 안의 모든 파일(하위 폴더 포함)이 업로더로 자동 전달되어 업로드됩니다. "
            "Chrome/Edge/Safari 데스크탑에서 작동합니다."
        )
        import streamlit.components.v1 as components
        inject_js = """
        <div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif;">
          <button id="pick-folder-btn"
            style="padding:10px 18px;background:#ff4b4b;color:white;border:none;
                   border-radius:8px;cursor:pointer;font-weight:600;font-size:14px;">
            📁 폴더 선택해서 모든 파일 업로드
          </button>
          <div id="pick-status" style="margin-top:8px;color:#555;font-size:13px;"></div>
          <script>
            const btn = document.getElementById('pick-folder-btn');
            const status = document.getElementById('pick-status');
            btn.addEventListener('click', () => {
              try {
                const parentDoc = window.parent.document;
                const uploaders = parentDoc.querySelectorAll(
                  'section[data-testid="stFileUploaderDropzone"] input[type="file"], ' +
                  'div[data-testid="stFileUploader"] input[type="file"]'
                );
                if (!uploaders.length) {
                  status.textContent = '❌ 페이지의 업로더를 찾지 못했어요. "ZIP으로 추가" 탭을 사용해주세요.';
                  return;
                }
                const target = uploaders[uploaders.length - 1];
                target.setAttribute('webkitdirectory', '');
                target.setAttribute('directory', '');
                target.setAttribute('mozdirectory', '');
                const cleanup = () => {
                  target.removeAttribute('webkitdirectory');
                  target.removeAttribute('directory');
                  target.removeAttribute('mozdirectory');
                };
                target.addEventListener('change', cleanup, { once: true });
                target.addEventListener('cancel', cleanup, { once: true });
                target.click();
                status.textContent = '폴더 선택 창에서 폴더를 고르면 자동 업로드됩니다...';
              } catch (e) {
                status.textContent = '❌ 브라우저 보안 정책으로 막혔어요: ' + e.message +
                  '. "ZIP으로 추가" 탭을 사용해주세요.';
              }
            });
          </script>
        </div>
        """
        components.html(inject_js, height=110)

        folder_files = st.file_uploader(
            "👆 위 버튼을 누르세요. 폴더 안 모든 파일이 여기에 채워집니다 "
            "(또는 직접 파일을 드래그&드롭 해도 됩니다).",
            type=["pdf", "docx", "doc", "pptx", "ppt", "png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            key="folder_uploader",
        )
        if folder_files:
            added = 0
            skipped = 0
            for f in folder_files:
                if Path(f.name).suffix.lower() not in SUPPORTED_CANDIDATE_EXTS:
                    skipped += 1
                    continue
                key = f"folder::{f.name}::{f.size}"
                if key not in st.session_state.uploaded_candidates:
                    st.session_state.uploaded_candidates[key] = {
                        "name": f.name,
                        "bytes": bytes(f.getbuffer()),
                        "size": f.size,
                        "relpath": f.name,
                    }
                    added += 1
            msg = f"{added}개 파일이 추가되었습니다."
            if skipped:
                msg += f" (지원하지 않는 형식 {skipped}개 건너뜀)"
            if added or skipped:
                st.success(msg)

    with tab_zip:
        st.caption(
            "폴더를 **ZIP**으로 압축해서 업로드하면 서버에서 자동으로 풀어 "
            "지원 파일들만 추출합니다. 가장 안정적인 방법입니다.\n\n"
            "**Windows**: 폴더 우클릭 → 보내기 → 압축(ZIP) 폴더\n"
            "**Mac**: 폴더 우클릭 → \"...\" 압축"
        )
        zip_files = st.file_uploader(
            "ZIP 파일 선택 (여러 개 가능)",
            type=["zip"],
            accept_multiple_files=True,
            key="zip_uploader",
        )
        if zip_files:
            total_added = 0
            total_skipped = 0
            errors = []
            for zf in zip_files:
                try:
                    with zipfile.ZipFile(io.BytesIO(bytes(zf.getbuffer()))) as zip_obj:
                        for info in zip_obj.infolist():
                            if info.is_dir():
                                continue
                            name = info.filename
                            try:
                                name = name.encode("cp437").decode("cp949")
                            except (UnicodeDecodeError, UnicodeEncodeError):
                                pass
                            ext = Path(name).suffix.lower()
                            if ext not in SUPPORTED_CANDIDATE_EXTS:
                                total_skipped += 1
                                continue
                            file_bytes = zip_obj.read(info)
                            display_name = Path(name).name
                            key = f"zip::{zf.name}::{name}"
                            if key not in st.session_state.uploaded_candidates:
                                st.session_state.uploaded_candidates[key] = {
                                    "name": display_name,
                                    "bytes": file_bytes,
                                    "size": len(file_bytes),
                                    "relpath": name,
                                }
                                total_added += 1
                except zipfile.BadZipFile:
                    errors.append(f"{zf.name}: 손상된 ZIP 파일")
                except Exception as e:
                    errors.append(f"{zf.name}: {e}")
            if total_added or total_skipped:
                msg = f"ZIP에서 {total_added}개 파일을 추가했습니다."
                if total_skipped:
                    msg += f" (지원하지 않는 형식 {total_skipped}개 건너뜀)"
                st.success(msg)
            for err in errors:
                st.error(err)

    with tab_path:
        is_replit = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEV_DOMAIN"))
        if is_replit:
            st.warning(
                "⚠️ 현재 앱이 **Replit 서버**에서 실행 중이에요. 이 탭은 "
                "Replit 서버 내부 경로만 볼 수 있고, 본인 PC 폴더는 보이지 않습니다. "
                "본인 PC 파일을 검색하려면 위의 **파일/폴더/ZIP** 탭을 이용하거나, "
                "이 앱을 본인 PC에서 `streamlit run app.py`로 직접 실행해주세요."
            )
        else:
            st.caption(
                "내 PC의 폴더 경로를 지정해 파일을 스캔합니다. "
                "(이 앱을 본인 PC에서 직접 실행할 때 사용)"
            )

        default_root = str(Path.home() if Path.home().exists() else Path.cwd())
        if "browser_path" not in st.session_state:
            st.session_state.browser_path = default_root

        current_path = Path(st.session_state.browser_path).expanduser()
        if not current_path.exists() or not current_path.is_dir():
            current_path = Path(default_root)
            st.session_state.browser_path = str(current_path)

        nav_cols = st.columns([1, 5, 1])
        with nav_cols[0]:
            if st.button(
                "⬆️ 상위", use_container_width=True,
                disabled=str(current_path) == str(current_path.parent),
                key="path_up_btn",
            ):
                st.session_state.browser_path = str(current_path.parent)
                st.rerun()
        with nav_cols[1]:
            typed_path = st.text_input(
                "현재 위치", value=str(current_path), key="browser_path_input",
                label_visibility="collapsed",
            )
        with nav_cols[2]:
            if st.button("이동", use_container_width=True, key="path_goto_btn"):
                target_path = Path(typed_path).expanduser()
                if target_path.exists() and target_path.is_dir():
                    st.session_state.browser_path = str(target_path)
                    st.rerun()
                else:
                    st.warning("존재하지 않는 폴더입니다.")

        try:
            subdirs = sorted(
                [p for p in current_path.iterdir() if p.is_dir() and not p.name.startswith(".")],
                key=lambda p: p.name.lower(),
            )
        except PermissionError:
            subdirs = []
            st.warning("이 폴더에 접근 권한이 없습니다.")
        except Exception as e:
            subdirs = []
            st.warning(f"폴더를 읽을 수 없습니다: {e}")

        enter_cols = st.columns([4, 1])
        with enter_cols[0]:
            subdir_names = ["(하위 폴더 선택해서 들어가기)"] + [p.name for p in subdirs]
            chosen = st.selectbox(
                "하위 폴더", subdir_names, index=0, key="subdir_select",
                label_visibility="collapsed",
            )
        with enter_cols[1]:
            if st.button(
                "들어가기", use_container_width=True,
                disabled=(chosen == "(하위 폴더 선택해서 들어가기)"),
                key="path_enter_btn",
            ):
                target = current_path / chosen
                if target.is_dir():
                    st.session_state.browser_path = str(target)
                    st.rerun()

        scan_opt_cols = st.columns([1, 1, 2])
        with scan_opt_cols[0]:
            recursive = st.checkbox("하위 폴더 포함", value=True, key="path_recursive")
        with scan_opt_cols[1]:
            max_scan_files = st.number_input(
                "최대 스캔 수", min_value=10, max_value=10000, value=500, step=50,
                key="path_max_scan",
            )

        if st.button(
            "🔍 이 폴더에서 지원 파일을 모두 추가", type="primary",
            use_container_width=True, key="path_scan_btn",
        ):
            scanned = []
            invalid = None
            try:
                iterator = current_path.rglob("*") if recursive else current_path.glob("*")
                for child in iterator:
                    if not child.is_file():
                        continue
                    if child.suffix.lower() in SUPPORTED_CANDIDATE_EXTS:
                        scanned.append(child)
                    if len(scanned) >= max_scan_files:
                        break
            except PermissionError:
                invalid = "접근 권한 없음"
            except Exception as e:
                invalid = f"스캔 실패: {e}"

            if invalid:
                st.error(invalid)
            elif not scanned:
                st.info("이 폴더에서 지원 파일을 찾지 못했습니다.")
            else:
                added = 0
                read_errors = []
                with st.spinner(f"{len(scanned)}개 파일 읽는 중..."):
                    for fp in scanned:
                        key = f"path::{fp}"
                        if key in st.session_state.uploaded_candidates:
                            continue
                        try:
                            data = fp.read_bytes()
                        except Exception as e:
                            read_errors.append(f"{fp.name}: {e}")
                            continue
                        st.session_state.uploaded_candidates[key] = {
                            "name": fp.name,
                            "bytes": data,
                            "size": len(data),
                            "relpath": str(fp.relative_to(current_path)) if fp.is_relative_to(current_path) else fp.name,
                        }
                        added += 1
                msg = f"{added}개 파일이 추가되었습니다."
                if len(scanned) >= max_scan_files:
                    msg += f" (최대 스캔 수 {max_scan_files}개 도달 — 더 많으면 위에서 값을 늘리세요)"
                st.success(msg)
                for e in read_errors:
                    st.error(e)

    # 선택된 파일 리스트
    st.markdown(f"**선택된 검색 대상 파일: {len(st.session_state.uploaded_candidates)}개**")
    if st.session_state.uploaded_candidates:
        total_size = sum(v["size"] for v in st.session_state.uploaded_candidates.values())
        cols = st.columns([5, 1])
        cols[0].caption(f"전체 용량: {format_size(total_size)}")
        if cols[1].button("전체 비우기", use_container_width=True):
            st.session_state.uploaded_candidates = {}
            st.rerun()
        with st.expander("파일 목록 보기 / 개별 제거", expanded=False):
            for key in list(st.session_state.uploaded_candidates.keys()):
                v = st.session_state.uploaded_candidates[key]
                row = st.columns([8, 1])
                row[0].markdown(f"- `{v['relpath']}` ({format_size(v['size'])})")
                if row[1].button("제거", key=f"rm_cand_{key}"):
                    del st.session_state.uploaded_candidates[key]
                    st.rerun()
    else:
        st.info("아직 추가된 파일이 없습니다. 위의 **파일 추가** 또는 **폴더 통째로 추가** 탭을 이용해주세요.")

    candidate_items: List[Dict[str, Any]] = list(st.session_state.uploaded_candidates.values())

    st.divider()
    st.header("검색 옵션")
    opt_cols = st.columns([2, 2, 1])
    with opt_cols[0]:
        top_k = st.slider("상위 결과 개수", min_value=3, max_value=30, value=10)
    with opt_cols[1]:
        zoom_label = st.selectbox(
            "PDF/문서 렌더링 해상도", list(ZOOM_OPTIONS.keys()), index=0
        )
        zoom = ZOOM_OPTIONS[zoom_label]
    with opt_cols[2]:
        st.write("")
        st.write("")
        run = st.button("검색 시작", type="primary", use_container_width=True)

    if not run:
        return

    if capture_file is None:
        st.warning("먼저 캡쳐 이미지를 업로드해주세요.")
        return
    if not candidate_items:
        st.warning("원본 후보 파일을 하나 이상 추가해주세요.")
        return

    try:
        capture_pil = load_capture_image(capture_file)
    except Exception as e:
        st.error(f"캡쳐 이미지를 열 수 없습니다: {e}")
        return
    capture_cv = pil_to_cv2(capture_pil)
    capture_data = {"pil": capture_pil, "cv": capture_cv}

    all_results: List[Dict[str, Any]] = []
    failed: List[Tuple[str, str]] = []

    progress = st.progress(0.0)
    progress_text = st.empty()
    summary = st.empty()

    total_files = len(candidate_items)
    processed_files = 0
    processed_pages = 0

    with tempfile.TemporaryDirectory() as temp_dir:
        for idx, item in enumerate(candidate_items, start=1):
            file_name = item["name"]
            ext = Path(file_name).suffix.lower()

            if ext not in SUPPORTED_CANDIDATE_EXTS:
                failed.append((file_name, f"지원하지 않는 형식: {ext}"))
                processed_files += 1
                progress.progress(processed_files / total_files)
                continue

            try:
                safe_name = Path(file_name).name
                saved_path = os.path.join(temp_dir, safe_name)
                base, sext = os.path.splitext(saved_path)
                i = 1
                while os.path.exists(saved_path):
                    saved_path = f"{base}_{i}{sext}"
                    i += 1
                with open(saved_path, "wb") as f:
                    f.write(item["bytes"])
            except Exception as e:
                failed.append((file_name, f"파일 저장 실패: {e}"))
                processed_files += 1
                progress.progress(processed_files / total_files)
                continue

            progress_text.write(f"({idx}/{total_files}) `{file_name}` 처리 중...")

            try:
                if ext in PDF_EXTS:
                    file_results = process_pdf_file(
                        saved_path, file_name, capture_data, zoom,
                        file_type_label="PDF", progress_text=progress_text,
                    )
                elif ext in OFFICE_EXTS:
                    file_results = process_office_file(
                        saved_path, file_name, capture_data, zoom,
                        progress_text=progress_text,
                    )
                elif ext in IMAGE_EXTS:
                    file_results = process_image_file(
                        saved_path, file_name, capture_data
                    )
                else:
                    file_results = []

                all_results.extend(file_results)
                processed_pages += len(file_results)
            except subprocess.TimeoutExpired:
                failed.append((file_name, "LibreOffice 변환 시간 초과"))
            except Exception as e:
                failed.append((file_name, f"{type(e).__name__}: {e}"))
                _ = traceback.format_exc()

            processed_files += 1
            progress.progress(processed_files / total_files)
            summary.write(
                f"처리 완료 파일: **{processed_files}/{total_files}**, "
                f"비교한 페이지/이미지 수: **{processed_pages}**"
            )

    progress_text.empty()
    progress.empty()

    st.divider()
    st.header("검색 결과")

    if not all_results:
        st.warning("비교 가능한 후보가 없습니다.")
    else:
        all_results.sort(key=lambda r: r["score"], reverse=True)
        filtered = [r for r in all_results if r["score"] >= 40]

        if not filtered:
            st.warning(
                "유사한 파일을 찾지 못했습니다. 아래는 가장 높은 점수를 받은 후보 3개입니다."
            )
            for i, r in enumerate(all_results[:3], start=1):
                render_result_card(i, r)
        else:
            shown = filtered[:top_k]
            st.success(
                f"총 **{len(filtered)}개**의 후보 중 상위 **{len(shown)}개**를 표시합니다."
            )
            for i, r in enumerate(shown, start=1):
                render_result_card(i, r)

    if failed:
        st.divider()
        st.subheader("처리 실패 파일")
        for name, reason in failed:
            st.error(f"**{name}** — {reason}")


if __name__ == "__main__":
    main()
