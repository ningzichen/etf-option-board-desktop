"""构建期预热导入：只为了让 PyInstaller 的静态分析看见运行时要用的依赖。

壳本身不 import 主项目（见 launcher/app.py 的职责边界），主项目又是在运行时
按 sys.path 从 build/core 加载的，PyInstaller 分析不到这些包。这个文件只是把
它们「列出来」，编译进产物后不执行任何逻辑。
"""

# 主项目 requirements.txt 的全部依赖
import fastapi  # noqa: F401
import httpx  # noqa: F401
import pypinyin  # noqa: F401
import uvicorn  # noqa: F401

# uvicorn 内部按字符串选实现，静态分析同样看不到
import uvicorn.logging  # noqa: F401
import uvicorn.loops.auto  # noqa: F401
import uvicorn.protocols.http.auto  # noqa: F401
import uvicorn.protocols.http.h11_impl  # noqa: F401
import uvicorn.protocols.websockets.auto  # noqa: F401
import uvicorn.lifespan.on  # noqa: F401

# 前端静态文件由 FastAPI 的 StaticFiles 托管
import starlette.staticfiles  # noqa: F401
import starlette.responses  # noqa: F401
