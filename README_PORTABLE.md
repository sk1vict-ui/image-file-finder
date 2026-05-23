# 캡쳐 이미지 원본 파일 찾기 — 포터블 사용 가이드

설치 없이 USB나 폴더에서 바로 실행할 수 있는 포터블 버전을 만드는 방법입니다.

## 빌드 방법 (한 번만)

**Windows PC**에서 다음 단계로 진행하세요:

1. 이 프로젝트 전체를 다운로드 (또는 git clone)
2. 폴더 안의 **`build_portable_windows.bat`** 를 더블클릭
3. 인터넷에서 Python(Embeddable)과 패키지를 자동 다운로드/설치 (5~10분 소요, 약 500MB)
4. 완료되면 `dist\CaptureFinder_Portable\` 폴더가 생성됨

## 사용 방법

빌드된 `dist\CaptureFinder_Portable\` 폴더 전체를 USB나 다른 PC로 복사한 뒤:

- 폴더 안의 **`run.bat`** 를 더블클릭
- 자동으로 브라우저가 열리고 앱이 실행됨
- 종료하려면 검은 콘솔 창을 닫으면 됨

**Python 설치 불필요** — 폴더 안에 포함된 임베디드 Python을 사용합니다.

## 파일 형식 지원

| 형식 | 포터블(Python만) | LibreOffice 추가 설치 시 |
|---|---|---|
| **PDF** | ✅ 모든 페이지 처리 | ✅ |
| **이미지** (PNG/JPG/JPEG/WEBP) | ✅ | ✅ |
| **PPTX / DOCX** | ✅ 내장 이미지 추출 매칭 | ✅ 페이지 전체 렌더링 매칭 (더 정확) |
| **PPT / DOC** (레거시) | ❌ 변환 불가 | ✅ |

### 레거시 .ppt / .doc 파일을 처리하려면

대상 PC에 [LibreOffice](https://www.libreoffice.org/download/download/)를 추가 설치하세요. 기본 경로(`C:\Program Files\LibreOffice\`)에 설치하면 앱이 자동 감지합니다.

또는 원본 파일을 **다른 이름으로 저장**해서 `.pptx` / `.docx`로 바꾼 뒤 사용하세요.

## 폴더 구조 (빌드 결과)

```
CaptureFinder_Portable/
├─ run.bat              ← 더블클릭해서 실행
├─ app.py
├─ .streamlit/
│  └─ config.toml
└─ python/              ← 임베디드 Python + 모든 패키지 (~500MB)
   ├─ python.exe
   ├─ Lib/site-packages/...
   └─ ...
```

## 트러블슈팅

- **방화벽이 차단됨**: 처음 실행 시 Windows Defender가 `python.exe`의 네트워크 접근을 묻습니다 → 허용
- **브라우저가 안 열림**: 직접 `http://localhost:8501` 로 접속
- **포트 충돌**: 다른 앱이 8501을 쓰면 `run.bat`의 `--server.port` 숫자를 바꾸세요
- **빌드 실패**: `build_cache\` 폴더를 지우고 다시 시도. 인터넷 연결 확인.

## 기술 요약

- **베이스**: Python 3.11 Embeddable (Windows x64)
- **UI**: Streamlit (로컬 브라우저)
- **PDF 렌더링**: PyMuPDF (순수 Python wheel, 외부 의존성 없음)
- **이미지 매칭**: OpenCV (ORB + Template) + ImageHash (pHash)
- **Office 처리**:
  - LibreOffice 있으면: 페이지 전체 PDF 변환 후 매칭 (정확도 높음)
  - 없으면: .pptx/.docx 내부 ZIP 구조에서 `ppt/media/`, `word/media/` 이미지 직접 추출
