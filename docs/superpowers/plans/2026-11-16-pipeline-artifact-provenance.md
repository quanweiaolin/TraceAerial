# Pipeline产物登记实现计划

> **给执行智能体的说明：** 必须使用子技能 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐个实现本计划。步骤用`- [ ]`勾选框语法追踪进度。

**目标：** 让`AD_pipeline_1`在处理完每个视频后，登记它最终的跟踪产物是什么、源自哪个原始视频、由哪个代码版本产出——在`aerial_db`里集中可查，且不要求`AD_pipeline_1`的进程架构做任何改动。

**架构：** `aerial_backend`新增一张`pipeline_artifacts`表和一个`POST /pipeline/artifacts`接口，按已有的`videos.storage_path`索引反查输入视频路径（在服务端完成，调用方永远不用自己算视频哈希）。新增一个无依赖、完全自包含的Python**单文件**（`register_artifact`）——不import任何其他`nds_code`模块，所以以后可以原样丢进任何同学的仓库——在被调用的那一刻现场探测自己的git commit（不依赖调用方的config/多进程架构），在`AD_pipeline_1`每个视频的stage循环跑完最后一步的那个位置POST给这个接口。注册失败永远不会挡住pipeline本身的产出：422（视频路径在`videos`里查不到）是数据问题，大声记日志（`logger.error`）；网络/后端不可达不是数据问题——会被追加进本地一个待重试文件，而不是直接丢弃，之后每次`register_artifact`被调用时都会先顺手尝试补发这个文件，这样一次短暂的服务中断会在下次pipeline跑起来的时候自动修复，不需要学生做任何事。

**技术栈：** 后端用FastAPI + SQLAlchemy(异步) + asyncpg；`AD_pipeline_1`那侧只用标准库(`urllib.request`、`subprocess`、`unittest`)——两边都不新增任何依赖。

**设计文档：** `docs/superpowers/specs/2026-11-16-pipeline-artifact-provenance-design.md`

## 全局约束

- 只在服务端做视频身份解析——`AD_pipeline_1`永远不自己算视频的sha256，始终只传它手头已有的绝对路径。
- `register_artifact`探测代码版本(git commit)的方式不能依赖`AD_pipeline_1`自身架构里任何特定的值/config字段/多进程传参方式（对应设计文档"方案比较"里选定的方案A，不是方案B）——必须只靠`register_artifact`自己所在的文件位置就能工作。
- 注册失败（网络不通、后端挂了）绝不能从`register_artifact`往外抛异常给调用方——pipeline真正的视频处理产出不能因为注册失败受影响。
- 422响应（输入路径在`videos.storage_path`里查不到）必须用`logger.error`记录，跟网络失败的处理方式明确区分——这是数据问题，不是临时性问题，绝不能写进待重试文件（重试也不会成功）。
- 网络/后端不可达这类失败，必须追加写进本地的待重试文件(JSON Lines格式)，不能只记日志就丢弃；`register_artifact`每次被调用时，必须先尝试补发这个文件(从旧到新，补发成功的就删掉)，再发送这次新的请求。
- `register_artifact`和它的待重试文件逻辑必须完全自包含在一个文件里，不import任何其他`nds_code`模块(只用标准库)——必须能原样复制粘贴进另一个同学的仓库，不能假设对方有特定的包结构。
- 这一轮只登记一个stage：`ad_perception_tracking`/"感知跟踪处理"，每个视频精确登记一次，就在`generate_standardized_output`返回之后（设计文档已确认：`standardized_output`永远会跑，不需要按条件分支处理）。
- `annotation_batches`的历史归档触发器、以及`input_paths`自动识别上游产物这个功能（设计文档里的"待办"），明确不在本计划范围内。
- 表结构里有`upstream_artifact_id`这一列（可为空），但本计划不会有任何代码去填它。
- `aerial_backend`目前没有任何自动化测试框架（没有pytest依赖，没有`conftest.py`）——按它现有的验证方式来（语法/import检查 + 对着跑起来的服务手动curl），不要单方面引入pytest。`AD_pipeline_1`那边已经在用标准库`unittest`（参见`nds_code/tests/test_output_layout.py`），新加的测试跟着这个惯例走。
- 数据库schema改动以`.sql`文件的形式交给用户自己执行（项目一贯的规矩——绝不自己执行数据库写操作），跟这个仓库里现有的那些根目录`add_*.sql`/`backfill_*.sql`文件是同一个惯例。

## Review Focus（容易漏测的点）

- 一个在`videos.storage_path`里找不到对应记录的视频路径（比如设计文档风险#1提到的bindfs路径和直连路径不一致），必须返回422并在消息里点名是哪个路径，客户端那边必须按error级别记日志，不能跟网络抖动一样悄悄吞掉。
- 一个请求里传了多个`input_video_paths`、但只有部分能解析出来，整个请求也必须422（设计文档："查不到就整体422报错"）——不允许部分成功，不允许`input_video_sha256`数组只填一半。
- 后端完全不可达（连接被拒/超时）时，`register_artifact`不能抛异常——`AD_pipeline_1`真正的pipeline产出无论如何都要正常跑完。
- 从一个完全没有`.git`目录的位置调用`register_artifact`（比如这个文件被单独拷出仓库之外）不能崩溃——应该用`code_commit=None`、`code_dirty=None`正常注册，而不是抛异常。
- 工作区有未提交改动（dirty）时也必须能正常注册成功，只是`code_dirty=true`——这是一个要记录下来的信号，不是用来拦截注册的理由。
- 之前某次调用的网络失败不能被遗忘：下一次`register_artifact`被调用时（哪怕是处理一个完全不相关的、更晚的视频），必须能在后端恢复后把它从待重试文件里成功重新发出去，发成功后要把这条从文件里删掉。

---

## 文件结构

**`aerial_backend/`（咱们自己的仓库）：**
- `add_pipeline_artifacts.sql`（新建，放在仓库根目录，跟现有的那些`add_*.sql`文件放一起）——`CREATE TABLE pipeline_artifacts` + 给`ad_perception_tracking`插入一条种子`pipeline_stages`记录。
- `app/models.py`（修改）——加`PipelineArtifact`这个ORM类。
- `app/api/routers/pipeline_artifacts.py`（新建）——`POST /pipeline/artifacts`：请求/响应的Pydantic模型，路径→sha256的反查逻辑，插入。
- `app/main.py`（修改）——加import + `include_router`。
- `tools/artifact_registration.py`（新建）——`detect_git_info(start_path: Path) -> tuple[str | None, bool | None]`，`register_artifact(stage_code: str, input_video_paths: list[str], output_path: str, metadata: dict, *, backend_base_url: str = DEFAULT_BACKEND_BASE_URL, pending_file: Path | None = None, code_commit_override: str | None = _UNSET) -> None`（`code_commit_override`用哨兵默认值区分"没传"(自动探测)和"显式传None"(补录古早数据，留空不猜)），外加一个本地待重试队列（`DEFAULT_PENDING_FILE`、`_flush_pending`）和一个`--retry-pending`命令行入口——完全自包含（只用标准库，不import任何其他模块）。**这个文件不放进`extern/AD_pipeline_1`**（那是外来的学生仓库，等同`extern/pds-drive-sync`的处理方式，不往里面塞我们自己的文件）——它是我们自己维护、按需复制/发给学生的工具，放在我们自己仓库里。
- `tools/tests/test_artifact_registration.py`（新建）——`unittest`写的测试，覆盖`detect_git_info`（真实的临时git仓库）和`register_artifact`的错误处理+待重试逻辑（mock掉`urllib`）。

**`extern/AD_pipeline_1/`（学生仓库，这次实现不改动它）：**
- Task 5描述的那一行`register_artifact(...)`调用，是**说明给学生的改法**（发给学生，由他自己加进他自己的仓库），不是我们直接commit进这份本地下载的快照里。

---

### Task 1: 后端schema——`pipeline_artifacts`表 + 种子stage记录

**Files:**
- Create: `aerial_backend/add_pipeline_artifacts.sql`

**Interfaces:**
- Produces: 表`aerial.pipeline_artifacts`，列为`id bigserial PK`、`stage_code text NOT NULL REFERENCES pipeline_stages(code)`、`input_video_sha256 bytea[] NOT NULL`、`code_repo text`、`code_commit text`、`code_dirty boolean`、`output_path text NOT NULL`、`upstream_artifact_id bigint REFERENCES pipeline_artifacts(id)`、`metadata jsonb NOT NULL DEFAULT '{}'`、`operator_name text`、`created_at timestamptz NOT NULL DEFAULT now()`；`input_video_sha256`上建一个GIN索引；在`pipeline_stages`里种一条记录`code='ad_perception_tracking'`、`name_cn='感知跟踪处理'`。
- Consumes: 已有的`aerial.pipeline_stages`和`aerial.videos`表（已在`app/models.py`里，本任务不改动它们）。

- [ ] **Step 1: 写`add_pipeline_artifacts.sql`**

```sql
SET search_path TO aerial, public;

INSERT INTO pipeline_stages (code, name_cn, stage_order, is_active, description)
VALUES ('ad_perception_tracking', '感知跟踪处理', 10, true,
        'AD_pipeline_1: 检测+稳像+对齐+跟踪+后处理+标准化输出，合并登记为一个stage')
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS pipeline_artifacts (
    id                   bigserial PRIMARY KEY,
    stage_code           text NOT NULL REFERENCES pipeline_stages(code),
    input_video_sha256   bytea[] NOT NULL,
    code_repo            text,
    code_commit          text,
    code_dirty           boolean,
    output_path          text NOT NULL,
    upstream_artifact_id bigint REFERENCES pipeline_artifacts(id),
    metadata             jsonb NOT NULL DEFAULT '{}',
    operator_name        text,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_artifacts_input_video_sha256
    ON pipeline_artifacts USING gin (input_video_sha256);
```

- [ ] **Step 2: 把这个文件交给用户**——不要自己执行。告诉他们文件已经在`aerial_backend/add_pipeline_artifacts.sql`，等他们自己执行（项目一贯规矩：不自己执行数据库写操作）。后面任务里的手动验证步骤都假设这一步已经跑过了。

- [ ] **Step 3: Commit**

```bash
git add aerial_backend/add_pipeline_artifacts.sql
git commit -m "feat: add pipeline_artifacts table + seed ad_perception_tracking stage"
```

---

### Task 2: 后端ORM模型

**Files:**
- Modify: `aerial_backend/app/models.py`（加在`AliyunFolderScan`之后，文件末尾）

**Interfaces:**
- Consumes: `Base`、`Mapped`、`mapped_column`、`ForeignKey`、`JSONB`、`ARRAY`、`LargeBinary`、`BigInteger`、`Text`、`Boolean`、`DateTime`、`func`——检查`models.py`文件头部现有的import，如果`ARRAY`(来自`sqlalchemy`)还没被import就补上。
- Produces: `PipelineArtifact`类，`__tablename__ = "pipeline_artifacts"`，列名/类型跟Task 1完全对应，供Task 3的接口使用。

- [ ] **Step 1: 往`app/models.py`加`PipelineArtifact`**

```python
class PipelineArtifact(Base):
    __tablename__ = "pipeline_artifacts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stage_code: Mapped[str] = mapped_column(ForeignKey("pipeline_stages.code"))
    input_video_sha256: Mapped[list[bytes]] = mapped_column(ARRAY(LargeBinary(32)))
    code_repo: Mapped[str | None] = mapped_column(Text)
    code_commit: Mapped[str | None] = mapped_column(Text)
    code_dirty: Mapped[bool | None] = mapped_column(Boolean)
    output_path: Mapped[str] = mapped_column(Text)
    upstream_artifact_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_artifacts.id")
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    operator_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 2: 确认模块能正常import**

Run: `cd aerial_backend && python3 -c "import ast; ast.parse(open('app/models.py').read())"`
Expected: 无输出(成功)，然后再跑`uv run python -c "import app.models"`，确认没有因为缺import（比如`ARRAY`没导入）而报`NameError`。

- [ ] **Step 3: Commit**

```bash
git add aerial_backend/app/models.py
git commit -m "feat: add PipelineArtifact ORM model"
```

---

### Task 3: 后端API接口

**Files:**
- Create: `aerial_backend/app/api/routers/pipeline_artifacts.py`
- Modify: `aerial_backend/app/main.py`

**Interfaces:**
- Consumes: `app.db.SessionContext`、`app.models.Video`、`app.models.PipelineArtifact`（Task 2）、SQLAlchemy的`select`。
- Produces: `router = APIRouter(prefix="/pipeline", tags=["pipeline"])`，挂一个`POST /pipeline/artifacts`，请求体字段`stage_code: str`、`input_video_paths: list[str]`、`output_path: str`、`code_repo: str | None`、`code_commit: str | None`、`code_dirty: bool | None`、`metadata: dict = {}`、`operator_name: str | None`；成功时返回`{"id": int, "input_video_sha256": [十六进制字符串...]}`；只要`input_video_paths`里有任何一个路径匹配不到`videos.storage_path`，就抛`HTTPException(422, ...)`，并点名所有匹配不到的路径。

- [ ] **Step 1: 写`app/api/routers/pipeline_artifacts.py`**

用一次查询把`input_video_paths`里所有路径都去`Video.storage_path`里查一遍（`select(Video.storage_path, Video.sha256).where(Video.storage_path.in_(input_video_paths))`），拼成一个`{path: sha256}`的映射；只要有任何请求路径不在这个映射里，就抛`HTTPException(422, detail=f"以下路径在videos表里查不到: {missing_paths}")`——这一步必须在插入任何记录之前完成（整体失败、不允许部分成功，对应Review Focus那条）。校验通过后，插入一条`PipelineArtifact`记录，`input_video_sha256=[map[p] for p in input_video_paths]`（保持调用方传入的顺序），返回`{"id": artifact.id, "input_video_sha256": [h.hex() for h in artifact.input_video_sha256]}`。

- [ ] **Step 2: 把这个router接进`app/main.py`**

在现有的router import那一行加上`from app.api.routers import pipeline_artifacts`，然后在其他`include_router`调用旁边加`app.include_router(pipeline_artifacts.router)`。

- [ ] **Step 3: 确认能正常import**

Run: `cd aerial_backend && uv run python -c "import app.main"`
Expected: 没有报错堆栈。

- [ ] **Step 4: 对着跑起来的服务手动验证**（这是本项目现有的验证惯例——后端没有pytest测试套件）

本地起服务，或者直接用生产环境正在跑的那个实例；找一条真实的`videos.storage_path`值和一条瞎编的路径，然后：

```bash
curl -X POST "http://127.0.0.1:8088/pipeline/artifacts" \
  -H "Content-Type: application/json" \
  -d '{"stage_code":"ad_perception_tracking","input_video_paths":["<真实的storage_path>"],"output_path":"/tmp/test_STD_TRK.csv","metadata":{}}'
```
Expected: 返回`200`，带一个`id`和正确的`input_video_sha256`十六进制值。

```bash
curl -X POST "http://127.0.0.1:8088/pipeline/artifacts" \
  -H "Content-Type: application/json" \
  -d '{"stage_code":"ad_perception_tracking","input_video_paths":["/not/a/real/path.MP4"],"output_path":"/tmp/x.csv","metadata":{}}'
```
Expected: 返回`422`，消息里点名`/not/a/real/path.MP4`。

```bash
curl -X POST "http://127.0.0.1:8088/pipeline/artifacts" \
  -H "Content-Type: application/json" \
  -d '{"stage_code":"ad_perception_tracking","input_video_paths":["<真实的storage_path>","/not/a/real/path.MP4"],"output_path":"/tmp/x.csv","metadata":{}}'
```
Expected: 返回`422`，只点名`/not/a/real/path.MP4`（整个请求失败，即使另一个路径是对的也不行——执行前后用`SELECT count(*) FROM pipeline_artifacts;`确认没有插入任何记录）。

- [ ] **Step 5: Commit**

```bash
git add aerial_backend/app/api/routers/pipeline_artifacts.py aerial_backend/app/main.py
git commit -m "feat: add POST /pipeline/artifacts endpoint"
```

---

### Task 4: 客户端helper——git探测 + 注册调用 + 待重试队列

**Files:**
- Create: `aerial_backend/tools/artifact_registration.py`（咱们自己仓库里，不是学生仓库——见"文件结构"）
- Test: `aerial_backend/tools/tests/test_artifact_registration.py`

**Interfaces:**
- Produces: `_GIT_SEARCH_ROOT = Path(__file__)`（模块级常量，`detect_git_info`/`_resolve_commit_override`默认的起点；测试通过`mock.patch('artifact_registration._GIT_SEARCH_ROOT', <tmp repo里的某个文件>)`来控制"当前代码仓库"指向哪，不需要真的搬动这个源文件）；`detect_git_info(start_path: Path) -> tuple[str | None, bool | None]`（返回`(commit_hash, is_dirty)`；如果`start_path`往上找不到任何`.git`目录，返回`(None, None)`）；`_UNSET`（模块级哨兵对象，`object()`即可，用来在`code_commit_override`上区分"没传这个参数"和"显式传了`None`"）；`_resolve_commit_override(value: str, start_path: Path) -> str | None`（用`git rev-parse --verify <value>^{commit}`解析，成功返回完整40位hash，失败返回`None`）；`register_artifact(stage_code: str, input_video_paths: list[str], output_path: str, metadata: dict, *, backend_base_url: str = "http://127.0.0.1:8088", pending_file: Path | None = None, code_commit_override: str | None = _UNSET) -> None`（永不抛异常；任何失败都只记日志然后返回；`pending_file`默认是`DEFAULT_PENDING_FILE`，只有测试会覆盖这个默认值；`code_commit_override`不传时走`detect_git_info`自动探测当前HEAD——跟以前行为一致；显式传`None`表示补录古早数据、不知道commit、不要猜，`code_commit`/`code_dirty`都留空；传一个字符串就走`_resolve_commit_override`解析，解析成功只填`code_commit`、`code_dirty`留空，解析失败记warning并同样留空，不阻止注册）；`DEFAULT_PENDING_FILE = Path.home() / ".aerial_pipeline" / "pending_artifacts.jsonl"`；一个`python -m artifact_registration --retry-pending`命令行入口。
- Consumes: 只用标准库（`subprocess`、`urllib.request`、`urllib.error`、`json`、`logging`、`pathlib.Path`、`argparse`）——不新增依赖（`nds_code/requirements.txt`里没有`requests`），不import任何其他`nds_code`模块。

- [ ] **Step 1: 在`nds_code/tests/test_artifact_registration.py`里写失败的测试**

```python
import json
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from artifact_registration import detect_git_info, register_artifact


class TestDetectGitInfo(unittest.TestCase):
    def test_clean_repo_returns_commit_and_not_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.email', 'a@b.c'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.name', 'a'], cwd=repo, check=True)
            (repo / 'f.txt').write_text('x')
            subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
            subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=repo, check=True)
            commit, dirty = detect_git_info(repo / 'f.txt')
            self.assertEqual(len(commit), 40)
            self.assertFalse(dirty)

    def test_dirty_repo_returns_dirty_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.email', 'a@b.c'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.name', 'a'], cwd=repo, check=True)
            (repo / 'f.txt').write_text('x')
            subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
            subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=repo, check=True)
            (repo / 'f.txt').write_text('changed')
            _, dirty = detect_git_info(repo / 'f.txt')
            self.assertTrue(dirty)

    def test_no_git_repo_returns_none_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            commit, dirty = detect_git_info(Path(tmp) / 'f.txt')
            self.assertIsNone(commit)
            self.assertIsNone(dirty)


class TestRegisterArtifact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pending_file = Path(self.tmp.name) / 'pending_artifacts.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    @mock.patch('artifact_registration.urlopen')
    def test_backend_unreachable_does_not_raise(self, mock_urlopen):
        mock_urlopen.side_effect = OSError('connection refused')
        register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                           pending_file=self.pending_file)  # 不能抛异常

    @mock.patch('artifact_registration.urlopen')
    def test_network_error_appends_to_pending_file(self, mock_urlopen):
        mock_urlopen.side_effect = OSError('connection refused')
        register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                           pending_file=self.pending_file)
        lines = self.pending_file.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['output_path'], '/b.csv')

    @mock.patch('artifact_registration.urlopen')
    def test_422_logs_error_and_is_not_added_to_pending(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError('url', 422, 'Unprocessable', {}, None)
        with self.assertLogs('artifact_registration', level='ERROR') as log_ctx:
            register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                               pending_file=self.pending_file)
        self.assertTrue(any('422' in m or 'Unprocessable' in m for m in log_ctx.output))
        self.assertFalse(self.pending_file.exists())

    @mock.patch('artifact_registration.urlopen')
    def test_network_error_logs_below_error_level(self, mock_urlopen):
        mock_urlopen.side_effect = OSError('timeout')
        with self.assertLogs('artifact_registration') as log_ctx:
            register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                               pending_file=self.pending_file)
        self.assertFalse(any(r.levelname == 'ERROR' for r in log_ctx.records))

    def test_next_call_flushes_pending_entry_once_backend_recovers(self):
        self.pending_file.parent.mkdir(parents=True, exist_ok=True)
        self.pending_file.write_text(json.dumps({
            'stage_code': 'ad_perception_tracking', 'input_video_paths': ['/old.MP4'],
            'output_path': '/old.csv', 'metadata': {},
        }) + '\n')
        with mock.patch('artifact_registration.urlopen') as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value.read.return_value = b'{"id": 1}'
            register_artifact('ad_perception_tracking', ['/new.MP4'], '/new.csv', {},
                               pending_file=self.pending_file)
            self.assertEqual(mock_urlopen.call_count, 2)  # 一次补发/old.csv，一次发/new.csv
        self.assertFalse(self.pending_file.exists())


class TestCodeCommitOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pending_file = Path(self.tmp.name) / 'pending_artifacts.jsonl'
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=self.repo, check=True)
        subprocess.run(['git', 'config', 'user.email', 'a@b.c'], cwd=self.repo, check=True)
        subprocess.run(['git', 'config', 'user.name', 'a'], cwd=self.repo, check=True)
        (self.repo / 'f.txt').write_text('x')
        subprocess.run(['git', 'add', '.'], cwd=self.repo, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', 'init'], cwd=self.repo, check=True)
        self.real_commit = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_override_autodetects_current_head(self):
        with mock.patch('artifact_registration.urlopen') as mock_urlopen:
            captured = {}
            def fake_urlopen(request, timeout=None):
                captured['body'] = json.loads(request.data)
                cm = mock.MagicMock()
                cm.__enter__.return_value.read.return_value = b'{"id": 1}'
                return cm
            mock_urlopen.side_effect = fake_urlopen
            with mock.patch('artifact_registration._GIT_SEARCH_ROOT', self.repo):
                register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                                   pending_file=self.pending_file)
        self.assertEqual(captured['body']['code_commit'], self.real_commit)

    def test_explicit_none_override_leaves_commit_and_dirty_blank(self):
        with mock.patch('artifact_registration.urlopen') as mock_urlopen:
            captured = {}
            def fake_urlopen(request, timeout=None):
                captured['body'] = json.loads(request.data)
                cm = mock.MagicMock()
                cm.__enter__.return_value.read.return_value = b'{"id": 1}'
                return cm
            mock_urlopen.side_effect = fake_urlopen
            register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                               pending_file=self.pending_file, code_commit_override=None)
        self.assertIsNone(captured['body']['code_commit'])
        self.assertIsNone(captured['body']['code_dirty'])

    def test_resolvable_short_hash_override_fills_full_commit(self):
        short_hash = self.real_commit[:8]
        with mock.patch('artifact_registration.urlopen') as mock_urlopen:
            captured = {}
            def fake_urlopen(request, timeout=None):
                captured['body'] = json.loads(request.data)
                cm = mock.MagicMock()
                cm.__enter__.return_value.read.return_value = b'{"id": 1}'
                return cm
            mock_urlopen.side_effect = fake_urlopen
            with mock.patch('artifact_registration._GIT_SEARCH_ROOT', self.repo):
                register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                                   pending_file=self.pending_file, code_commit_override=short_hash)
        self.assertEqual(captured['body']['code_commit'], self.real_commit)
        self.assertIsNone(captured['body']['code_dirty'])

    def test_unresolvable_override_warns_and_still_registers(self):
        with mock.patch('artifact_registration.urlopen') as mock_urlopen:
            cm = mock.MagicMock()
            cm.__enter__.return_value.read.return_value = b'{"id": 1}'
            mock_urlopen.return_value = cm
            with mock.patch('artifact_registration._GIT_SEARCH_ROOT', self.repo):
                with self.assertLogs('artifact_registration', level='WARNING'):
                    register_artifact('ad_perception_tracking', ['/a.MP4'], '/b.csv', {},
                                       pending_file=self.pending_file,
                                       code_commit_override='not-a-real-hash')
        self.assertEqual(mock_urlopen.call_count, 1)  # 仍然发出了注册请求
```

- [ ] **Step 2: 跑测试，确认它们会失败**

Run: `cd extern/AD_pipeline_1 && PYTHONDONTWRITEBYTECODE=1 python -B -m unittest nds_code.tests.test_artifact_registration -v`
Expected: `ModuleNotFoundError: No module named 'artifact_registration'`

- [ ] **Step 3: 实现`detect_git_info`、`register_artifact`、以及待重试文件的补发逻辑**

`detect_git_info(start_path)`：从`start_path.parent`开始往上找`.git`目录（不要依赖`cwd`）；找不到就返回`(None, None)`。找到了就以这个仓库根目录为`cwd`跑`git rev-parse HEAD`和`git status --porcelain`（`capture_output=True`、`text=True`、`timeout=5`）；`is_dirty`就是`bool(status_output.strip())`。整个函数体包在`try/except (OSError, subprocess.SubprocessError)`里，任何失败都返回`(None, None)`——git不存在或者行为异常绝不能让调用方崩溃。

一个私有的`_post(payload: dict, backend_base_url: str) -> None`负责真正发请求：`urllib.request.Request(f'{backend_base_url}/pipeline/artifacts', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')` + `urlopen(request, timeout=10)`（要直接`from urllib.request import urlopen`这样import，这样测试里的`mock.patch('artifact_registration.urlopen')`才能生效），异常原样往外抛——由调用方决定怎么处理`urllib.error.HTTPError`和其他异常。

一个私有的`_flush_pending(pending_file: Path, backend_base_url: str) -> None`：如果`pending_file`不存在就直接返回。否则逐行读取，每一行尝试`_post(json.loads(line), backend_base_url)`：成功就丢掉这一行；如果是`HTTPError`且`code == 422`，记一条`logger.error(...)`然后也丢掉这一行（重试也没用）；其他任何异常就保留这一行（还是pending状态），并且这次调用就不再往下尝试剩下的行了（保持顺序，避免对着一个还没恢复的后端反复发请求）。最后把`pending_file`重写成只剩下还pending的那些行，如果一行都不剩就直接删掉这个文件。

`_resolve_commit_override(value, start_path)`：跟`detect_git_info`共用同一套"从`start_path.parent`往上找`.git`"逻辑（可以提取成一个共享的私有helper`_find_git_root(start_path) -> Path | None`给两边复用）；找不到仓库，或者`subprocess.run(['git', 'rev-parse', '--verify', f'{value}^{{commit}}'], cwd=repo_root, capture_output=True, text=True, timeout=5)`返回非0，都返回`None`；成功就返回`result.stdout.strip()`（完整40位hash）。

`register_artifact(stage_code, input_video_paths, output_path, metadata, *, backend_base_url=DEFAULT_BACKEND_BASE_URL, pending_file=None, code_commit_override=_UNSET)`：先确定`pending_file = pending_file or DEFAULT_PENDING_FILE`；调用`_flush_pending(pending_file, backend_base_url)`并吞掉它可能抛出的任何异常（尽力而为，补发失败也不能挡住这次新产物的注册）。接下来决定`code_commit`/`code_dirty`这两个值：
- `code_commit_override is _UNSET`（没传这个参数，正常跑pipeline时的默认情况）→ `code_commit, code_dirty = detect_git_info(_GIT_SEARCH_ROOT)`，跟以前行为一致。
- `code_commit_override is None`（显式传了`None`，补录古早数据、确实想不起来是哪个commit）→ `code_commit, code_dirty = None, None`，**不**调用`detect_git_info`。
- 其他情况（传了具体字符串）→ `resolved = _resolve_commit_override(code_commit_override, _GIT_SEARCH_ROOT)`；解析成功就`code_commit, code_dirty = resolved, None`；解析失败就`logger.warning(...)`点名这个值解析不了，`code_commit, code_dirty = None, None`，但照常继续往下注册，不中断。

拼出JSON请求体（`stage_code`、`input_video_paths`、`output_path`、`metadata`、`operator_name=os.environ.get('USER')`，外加上面算出来的`code_commit`/`code_dirty`，`code_repo`取`_find_git_root`结果所在目录的名字）；调用`_post(body, backend_base_url)`；如果是`HTTPError`且`code == 422`，就`logger.error(...)`记下响应内容然后返回；其他任何异常，就`logger.warning(...)`然后把`body`追加成一行JSON写进`pending_file`（先用`mkdir(parents=True, exist_ok=True)`确保父目录存在）。

加一个`if __name__ == '__main__':`代码块，用`argparse`支持`--retry-pending`这个flag，调用`_flush_pending(DEFAULT_PENDING_FILE, DEFAULT_BACKEND_BASE_URL)`，并打印还剩多少条没发出去。

- [ ] **Step 4: 跑测试，确认全部通过**

Run: `cd extern/AD_pipeline_1 && PYTHONDONTWRITEBYTECODE=1 python -B -m unittest nds_code.tests.test_artifact_registration -v`
Expected: `OK`（12个测试）。

- [ ] **Step 5: Commit**

```bash
git add aerial_backend/tools/artifact_registration.py nds_code/tests/test_artifact_registration.py
git commit -m "feat: add register_artifact client helper with self-contained git detection and pending-retry queue"
```

---

### Task 5: 把这一处注册调用接进`main.py`

**这是交给学生自己改的说明，不是我们在本地那份`extern/AD_pipeline_1`快照里执行的步骤**——那是学生自己的仓库（等同`extern/pds-drive-sync`的外来仓库处理方式），我们不往里面提交改动。下面的"Files"是指学生仓库里的文件。

**Files:**
- Modify（学生自己操作）: `nds_code/main.py:1610`（紧接在现有的`logger.info(f'[{video_name}] [GPU{gpu_id}] 标准化输出完成: {std_trk_path}')`这一行之后，仍在`if run_func.get('standardized_output'):`这个代码块内，`check_shutdown('standardized_output_done')`之前）

**Interfaces:**
- Consumes: Task 4的`register_artifact`（来自`artifact_registration`）；`config['raw_single_video_path']`、`std_trk_path`、`meta_path`（根据设计阶段读过的代码，这几个变量在插入点已经在作用域内）。

- [ ] **Step 1: 在`main.py`靠前的位置加上import**（跟这个函数内部其他`from nds_code.utils...`的import放一起，比如紧跟在第1597行那个现有的`from nds_code.utils.step3_postprocess.standardize_output import generate_standardized_output`之后）

```python
from artifact_registration import register_artifact
```

- [ ] **Step 2: 加这次调用**

```python
register_artifact(
    'ad_perception_tracking',
    [config['raw_single_video_path']],
    std_trk_path,
    {'meta_path': meta_path},
)
```
放在`logger.info(...标准化输出完成...)`那一行之后。

- [ ] **Step 3: 确认模块还能正常解析和import**

Run: `cd extern/AD_pipeline_1 && python3 -c "import ast; ast.parse(open('nds_code/main.py').read())"`
Expected: 无输出(成功)。

- [ ] **Step 4: 拿一个真实视频做手动冒烟测试**——用现有的pipeline入口（`python nds_code/main.py run -c <任意一个现成的场景配置>.yaml`）对一个视频跑一遍完整流程，同时让Task 3的后端处于运行状态（本地或共享服务器），然后用`curl`或者直接`psql`确认`pipeline_artifacts`里刚好多了一条记录，`output_path`符合预期、`code_commit`不是空的。

Run: `psql "postgresql://reader:<密码>@localhost:5432/aerial_db" -c "SET search_path TO aerial, public;" -c "SELECT id, stage_code, output_path, code_commit, code_dirty, created_at FROM pipeline_artifacts ORDER BY id DESC LIMIT 1;"`
Expected: 返回一条记录，跟刚处理的那个视频对得上。

- [ ] **Step 5: Commit**

```bash
git add nds_code/main.py
git commit -m "feat: register ad_perception_tracking artifact after standardized_output"
```

---

## Self-Review

1. **设计文档覆盖**：stage颗粒度（合并成一个stage，Task 1种子记录）✓；代码版本探测用方案A、自包含探测（Task 4）✓；输入路径→sha256服务端解析 + 整体422（Task 3）✓；其余信息全塞进JSONB metadata（Task 3的`metadata`原样传递，Task 5的`{'meta_path': meta_path}`）✓；`upstream_artifact_id`列已建但不填（Task 1/2只建schema）✓；注册失败不挡住真正的pipeline产出（Task 4的try/except设计）✓；本地待重试队列 + 下次调用自动补发 + `--retry-pending`命令行入口（Task 4）✓；单文件完全自包含、不import任何`nds_code.*`（Task 4的Interfaces + 全局约束）✓；"待办"（annotation_batches历史触发器、input_paths自动识别上游产物）已在全局约束里明确排除✓。
2. **步骤扫描**：没有任何一步留"TBD"或"处理边界情况"这种没下决定的话——每个实现步骤都点名了具体函数、具体的控制流（try/except的边界、抓哪些异常、用哪个日志级别），每个测试步骤都有具体的断言。
3. **类型一致性**：`register_artifact`的第四个位置参数在全文都是`metadata: dict`（Task 4的签名、Task 4的测试、Task 5的调用处）——一致。`detect_git_info`在Task 4的接口说明和它的三个测试里返回的都是`tuple[str | None, bool | None]`。`PipelineArtifact.metadata_`（Python侧属性名，映射到数据库的`metadata`列）跟Task 1 SQL、Task 3请求字段名里用的`"metadata"`对得上。`pending_file: Path | None = None`在Task 4的接口说明、`TestRegisterArtifact`里每个测试（全部显式传了这个参数）、以及`_flush_pending`的签名里都一致。
4. **Review Focus覆盖**：六条都有对应的验证步骤——路径查不到返回422（Task 3 Step 4，第二条curl）；部分匹配时整体422（Task 3 Step 4，第三条curl，明确检查了记录数没变化）；后端不可达不抛异常（Task 4的`test_backend_unreachable_does_not_raise`）；没有`.git`时优雅降级（Task 4的`test_no_git_repo_returns_none_none`）；工作区dirty照样能注册成功（Task 4的`test_dirty_repo_returns_dirty_true`）；之前失败的网络请求会在下次调用时被重新发出并清空（Task 4的`test_next_call_flushes_pending_entry_once_backend_recovers`）。补充一条：古早数据补录时"不传"vs"显式传None"vs"传一个能/不能解析的值"这四种组合，分别由`TestCodeCommitOverride`下的四个测试覆盖，避免这个后加的功能本身成为新的未测路径。

5. **篇幅比例**：五个任务，每个任务对应一个文件量级的新代码；代码块都是测试体（精确固定行为）和唯一一个不那么直白的算法（git往上找仓库），不是把每个函数原样抄一遍——跟设计文档的篇幅大致成比例。
