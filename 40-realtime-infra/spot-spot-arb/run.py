"""서버 실행 스크립트.

프로젝트 루트에서 실행:
    python run.py

또는:
    uvicorn backend.main:app --reload
"""

import uvicorn

if __name__ == '__main__':
    uvicorn.run('backend.main:app', host='0.0.0.0', port=8000, reload=True)
