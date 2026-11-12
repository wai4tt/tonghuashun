"""Export the current working tree using the competition S4 delivery layout.

Reads source files and ORM definitions only; never connects to the user database.
Run with the project's Python: python scripts/package_prototype.py
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE_DIRS = ("backend", "frontend", "scripts", "tests", "docs", "examples", ".agents/skills")
SOURCE_FILES = ("README.md", "requirements.txt", "conftest.py", ".gitignore", ".env.example", ".streamlit/config.toml")
EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".venv", ".git", "node_modules"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_inventory() -> dict[str, bytes]:
    paths = [ROOT / name for name in SOURCE_FILES]
    for directory in SOURCE_DIRS:
        paths.extend(path for path in (ROOT / directory).rglob("*") if path.is_file())
    result = {}
    for path in sorted(set(paths)):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES or path.name == "last_run_report.json":
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            continue
        if path.is_symlink():
            raise ValueError(f"Source symlink requires review: {relative}")
        result[relative.as_posix()] = path.read_bytes()
    return result


def check_secrets(files: dict[str, bytes]) -> None:
    # Compare with local credentials without printing their values or exporting them.
    from dotenv import dotenv_values

    sensitive = []
    for name in (".env", ".env.admin"):
        path = ROOT / name
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if value and len(value) >= 8 and any(word in key.upper() for word in ("KEY", "SECRET", "PASSWORD", "TOKEN", "MYSQL_URL")):
                sensitive.append(value.encode("utf-8"))
    leaked = [name for name, data in files.items() if any(value in data for value in sensitive)]
    if leaked:
        raise ValueError("Local credential found in source: " + ", ".join(leaked))


def mysql_schema() -> tuple[str, list[dict]]:
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateIndex, CreateTable
    from backend.app.database import Base

    dialect = mysql.dialect()
    statements = [
        "-- 问策智投 MySQL 8.0+ 空库初始化脚本；由当前 ORM 表结构生成。",
        "-- 不包含账号、密码哈希、对话、画像或任何正式用户数据。",
        "-- 面向新建数据库；已有表的升级请使用单独迁移流程。",
        "SET NAMES utf8mb4;",
        "CREATE DATABASE IF NOT EXISTS `wence_zhitou` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        "USE `wence_zhitou`;",
    ]
    tables = []
    for table in Base.metadata.sorted_tables:
        sql = str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip()
        indexes = []
        for index in sorted(table.indexes, key=lambda item: item.name):
            index_sql = str(CreateIndex(index).compile(dialect=dialect))
            match = re.fullmatch(r"CREATE (UNIQUE )?INDEX (.+?) ON .+? (\(.+\))", index_sql)
            if not match:
                raise ValueError(f"Unsupported index SQL for {index.name}")
            indexes.append(f"\t{match[1] or ''}INDEX {match[2]} {match[3]}")
        if indexes:
            body, end = sql.rsplit("\n)", 1)
            sql = body.rstrip() + ",\n" + ",\n".join(indexes) + "\n)" + end
        statements.append(sql + " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;")
        tables.append({"name": table.name, "columns": [column.name for column in table.columns],
                       "indexes": [index.name for index in sorted(table.indexes, key=lambda item: item.name)]})
    return "\n\n".join(statements) + "\n", tables


def sqlite_schema() -> str:
    # Extract literal initialization statements from the actual constructors.
    files = ("backend/app/shared_sessions.py", "backend/app/services/public_fact_store.py")
    chunks = ["-- SQLite 运行时缓存结构参考；与 MySQL 业务库分开。",
              "-- 应用自动创建这些表，不需要手工导入；两个源码文件对应不同 SQLite 文件。"]
    for filename in files:
        chunks.append(f"\n-- 来源：{filename}")
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute" and node.args:
                statement = node.args[0]
                if isinstance(statement, ast.Constant) and isinstance(statement.value, str) and statement.value.startswith("CREATE "):
                    chunks.append(statement.value + ";")
    return "\n".join(chunks) + "\n"


STARTUP = """# 问策智投源码运行说明

解压 `问策智投_完整源码.zip`，进入其中的 `问策智投` 目录。前后端共用这个项目根目录，请保持内部相对路径。

环境：Python 3.11+、MySQL 8.0+；第三方模型、问财实时取数需自行配置有效凭据。

```powershell
python -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，填写 MySQL 账号和密码，并将 `WENCE_AUTH_SECRET` 改为至少 32 位的独立随机字符串。需要完整研究时再填写模型及问财 API 配置。

用 MySQL 客户端导入同级交付材料 `数据库脚本/数据库.sql`。该脚本创建 `wence_zhitou` 空库和表；`.env` 的 `MYSQL_DATABASE` 应保持一致。也可以先自行建空库，由后端启动时自动建表。

在项目根目录打开两个终端，分别执行：

```powershell
.\\.venv\\Scripts\\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

```powershell
.\\.venv\\Scripts\\python.exe -m streamlit run frontend/streamlit_app.py --server.port 8501
```

打开 http://127.0.0.1:8501，首次使用自行注册账号。API 文档：http://127.0.0.1:8000/docs；健康检查：http://127.0.0.1:8000/api/v1/health。

可选管理员：在数据库配置完成后执行 `python scripts/create_admin.py --username admin`，新凭据写入本机 `.env.admin`。交付包不预置管理员密码。

测试：`python -m pytest -q tests`。真实模型、问财与 MySQL 联调需要独立运行相应验证脚本；单元测试通过不表示外部服务已连通。

默认 lite 仅处理已有资料及固定规则；完整语义、实时资料和专业研究需使用 deep 并配置服务。缺失证据会保留待复核，系统不自动下单。

完整技术与配置说明见解压后的 `README.md` 和 `docs/`。
"""


def build(destination: Path) -> dict:
    archive = destination.with_suffix(".zip")
    hashfile = archive.with_suffix(".zip.sha256")
    if any(path.exists() for path in (destination, archive, hashfile)):
        raise FileExistsError("Output already exists; choose a new --output directory.")
    sources = source_inventory()
    check_secrets(sources)
    # Fail before creating output if any Python source cannot be parsed.
    for name, data in sources.items():
        if name.endswith(".py"):
            compile(data, name, "exec")
    sql, tables = mysql_schema()
    source_dir = destination / "S4A_源码及数据库脚本" / "源码"
    database_dir = destination / "S4A_源码及数据库脚本" / "数据库脚本"
    demo_dir = destination / "S4_B演示视频"
    for path in (source_dir, database_dir, demo_dir / "系统快照" / "网站快照", demo_dir / "系统快照" / "移动端快照"):
        path.mkdir(parents=True)
    source_archive = source_dir / "问策智投_完整源码.zip"
    with ZipFile(source_archive, "w", ZIP_DEFLATED) as zipped:
        for name, data in sources.items():
            zipped.writestr("问策智投/" + name, data)
        zipped.writestr("问策智投/运行说明.md", STARTUP.encode("utf-8"))
    (source_dir / "运行说明.md").write_text(STARTUP, encoding="utf-8")
    (database_dir / "数据库.sql").write_text(sql, encoding="utf-8")
    (database_dir / "SQLite运行时缓存结构.sql").write_text(sqlite_schema(), encoding="utf-8")
    (database_dir / "数据库说明.md").write_text(
        "# 数据库脚本说明\n\n`数据库.sql` 对应当前 `backend/app/database.py` 的 10 张 MySQL 业务表，含主键、外键、唯一约束及索引。仅初始化空库，不导出业务记录，不连接现有数据库。\n\n"
        "MySQL 8.0+ 客户端：先连接自己的数据库服务器，再执行 `SOURCE /绝对路径/数据库.sql;`（中文或空格路径可用图形客户端打开脚本执行）。\n\n"
        "数据库名默认为 `wence_zhitou`，修改时需同步 `.env`。脚本不创建数据库登录用户；请使用自己的账号。应用启动会自动补建缺失表；旧表字段变更仍需专门迁移。\n\n"
        "Python ORM 的 `default` / `onupdate` 由应用写入，不等于数据库服务器默认值。\n\n"
        "`SQLite运行时缓存结构.sql` 仅供结构查阅：登录租约与公开资料缓存分别存放在两个 SQLite 文件中，应用运行时自动创建。无需将其导入 MySQL，也无需携带旧缓存。\n", encoding="utf-8")
    media = {
        "问策智投_提交材料/问策智投_项目演示视频.mp4": "问策智投_项目演示视频.mp4",
        ".streamlit/login-desktop.png": "系统快照/网站快照/登录页_已有截图.png",
        ".streamlit/login-mobile.png": "系统快照/移动端快照/登录页_已有截图.png",
    }
    media_records = []
    for source_name, target_name in media.items():
        source = ROOT / source_name
        if source.is_file():
            data = source.read_bytes()
            (demo_dir / target_name).write_bytes(data)
            media_records.append({"source": source_name, "target": target_name, "sha256": digest(data),
                                  "original_modified_at": datetime.fromtimestamp(source.stat().st_mtime, ZoneInfo("Asia/Shanghai")).isoformat()})
    (demo_dir / "素材说明.md").write_text(
        "# 演示素材说明\n\n本目录复用项目已有演示视频及桌面、移动端登录截图，本次未重新录制。它们不代表当前源码的最新界面或完整功能验收。\n\n"
        "项目为响应式网页，没有对应小程序，因此按网站快照、移动端快照分类。原始素材路径、修改时间和 SHA-256 见 `素材来源.json`。\n", encoding="utf-8")
    (demo_dir / "素材来源.json").write_text(json.dumps(media_records, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "source_file_count": len(sources), "python_file_count": sum(name.endswith(".py") for name in sources),
        "source_directories": SOURCE_DIRS, "source_files": SOURCE_FILES, "database_tables": tables,
        "excluded": ["真实 .env / .env.admin", "正式用户数据", "虚拟环境", "Git 历史", "运行缓存", "日志", "临时文件", "旧测试结果", "无关本地素材"],
        "media": media_records,
    }
    (destination / "整理清单.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(("原项目相对路径", "源码压缩包内路径", "字节数", "SHA256"))
    for name, data in sources.items():
        writer.writerow((name, "问策智投/" + name, len(data), digest(data)))
    (destination / "源码文件清单.csv").write_text(buffer.getvalue(), encoding="utf-8-sig")
    (destination / "交付说明.md").write_text(
        f"# 问策智投 · S4 原型演示\n\n整理日期：{summary['created_at'][:10]}。以当前工作区源码为准，包含尚未提交 Git 的项目源码。\n\n"
        "```text\nS4_原型演示/\n├─ S4A_源码及数据库脚本/\n│  ├─ 源码/\n│  │  ├─ 问策智投_完整源码.zip\n│  │  └─ 运行说明.md\n│  └─ 数据库脚本/\n│     ├─ 数据库.sql\n│     ├─ SQLite运行时缓存结构.sql\n│     └─ 数据库说明.md\n├─ S4_B演示视频/\n│  ├─ 问策智投_项目演示视频.mp4\n│  ├─ 系统快照/网站快照/\n│  └─ 系统快照/移动端快照/\n├─ 源码文件清单.csv\n└─ 整理清单.json\n```\n\n"
        f"源码包共 {len(sources)} 个原项目文件；保持 backend、frontend、scripts、tests、docs、examples、.streamlit、.agents/skills 的原有相对路径，避免拆分前后端后导入失败。源码包另附运行说明。\n\n"
        "数据库脚本由当前 ORM 生成，包含 10 张业务表及 SQLite 缓存结构参考；不含真实账号和业务数据。真实密钥、用户库、虚拟环境、日志与缓存均排除。\n\n"
        "演示视频和截图为已有素材，来源见演示目录说明。本次代码整理不等同于重新录制或真实外部服务验收。\n\n"
        "先阅读 `S4A_源码及数据库脚本/源码/运行说明.md`。源码完整性可按 `源码文件清单.csv` 的 SHA-256 逐文件核对。\n", encoding="utf-8")
    with ZipFile(source_archive) as zipped:
        assert zipped.testzip() is None
        for name, data in sources.items():
            assert zipped.read("问策智投/" + name) == data
    with ZipFile(archive, "w", ZIP_DEFLATED) as zipped:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                zipped.write(path, (Path(destination.name) / path.relative_to(destination)).as_posix())
    with ZipFile(archive) as zipped:
        assert zipped.testzip() is None
    hashfile.write_text(f"{digest(archive.read_bytes())}  {archive.name}\n", encoding="utf-8")
    return {"directory": str(destination), "archive": str(archive), "source_files": len(sources), "tables": len(tables)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "deliverables" / "S4_原型演示")
    args = parser.parse_args()
    print(json.dumps(build(args.output.resolve()), ensure_ascii=False, indent=2))
