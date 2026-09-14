"""
VRU检测QA人工审核：给人看抽帧+检测叠加图，判断这一帧检测得对不对。

存储沿用之前定的方案(先json，不进库，见对话记录)：
  - 批次清单(哪一天跑了哪些) 在 services/vru_detection._write_batch_manifest 写
  - review结果(通过/不通过) 就近写在各自视频的 Test/ 目录下，
    见 services/vru_detection.write_review/read_review

这个路由只做：读清单+读review状态拼给前端、提供图片文件、接收review提交。
不碰数据库(候选筛选/跑检测那部分在 app/main.py 的 /qa/run-batch 里)。
"""
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.services.vru_detection import (
    PROCESSING_ROOT,
    detect_with_model_on_frame,
    get_default_model_filename,
    get_or_create_thumbnail,
    list_available_models,
    list_batch_dates,
    read_batch_manifest,
    read_review,
    write_review,
)

router = APIRouter(prefix="/qa", tags=["qa_review"])


def _resolve_allowed_path(path: str) -> Path:
    """校验路径必须在Aerial_Processing_Data(或本地测试替代目录)根下，
    防止/qa/image、/qa/thumbnail被当成任意文件读取接口。"""
    p = Path(path)
    try:
        p.relative_to(PROCESSING_ROOT)
    except ValueError:
        raise HTTPException(403, "path not under allowed root")
    if not p.is_file():
        raise HTTPException(404, "file not found")
    return p


@router.get("/batches")
async def get_batches():
    """列出有哪些批次(日期)可以审核，最新的排在最前面。"""
    return {"dates": list_batch_dates()}


@router.get("/batches/{date_str}")
async def get_batch_items(date_str: str):
    """返回某一天批次的所有条目，每条附带当前review状态(没标注过是null)。"""
    manifest = read_batch_manifest(date_str)
    if manifest is None:
        raise HTTPException(404, f"batch {date_str} not found")

    items = []
    for item in manifest["items"]:
        review = read_review(
            item["test_dir"], item["frame_idx"], item["model_name"], item["model_version"]
        )
        items.append({**item, "review": review})
    return {"date": date_str, "items": items}


@router.get("/image")
async def get_image(path: str):
    """按绝对路径读原图返回(完整分辨率)。仅限Aerial_Processing_Data根目录下的文件。"""
    p = _resolve_allowed_path(path)
    return FileResponse(p)


@router.get("/thumbnail")
async def get_thumbnail(path: str):
    """跟/qa/image一样校验路径，但返回缩小过的缓存版本，给页面<img>用——
    原图4K截帧几MB一张，直接传给浏览器在弱网(比如tailscale远程访问)下容易
    加载超时/失败。第一次请求某张图时现算缩略图存/tmp，之后直接复用。"""
    p = _resolve_allowed_path(path)
    thumb_path = await asyncio.to_thread(get_or_create_thumbnail, str(p))
    return FileResponse(thumb_path)


class ReviewItemIn(BaseModel):
    test_dir: str
    frame_idx: int
    model_name: str
    model_version: str
    verdict: str  # "pass" / "fail"
    note: str = ""


class SubmitReviewsIn(BaseModel):
    reviewed_by: str | None = None
    items: list[ReviewItemIn]


@router.post("/submit")
async def submit_reviews(body: SubmitReviewsIn):
    """批量提交review结果，一次可以是一条也可以是勾选的一批。"""
    for item in body.items:
        write_review(
            item.test_dir,
            item.frame_idx,
            item.model_name,
            item.model_version,
            verdict=item.verdict,
            reviewed_by=body.reviewed_by,
            note=item.note,
        )
    return {"updated": len(body.items)}


@router.get("/models")
async def get_models():
    """列出checkpoints目录下有哪些.pt模型，前端下拉框用来选"拿哪个模型对比"。"""
    return {"models": list_available_models(), "default": get_default_model_filename()}


class DetectRequestIn(BaseModel):
    test_dir: str
    frame_idx: int
    raw_image: str
    model_filename: str


@router.post("/detect")
async def detect_item(body: DetectRequestIn):
    """用指定模型对某一帧现场跑一次检测(帧是复用已经抽好的raw_image，不重新抽)，
    给前端"这个模型还没检测过这一帧"时点"检测"按钮用。"""
    try:
        result = await asyncio.to_thread(
            detect_with_model_on_frame,
            body.raw_image, body.test_dir, body.frame_idx, body.model_filename,
        )
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))
    return result


@router.get("/review", response_class=HTMLResponse)
async def review_page():
    return _REVIEW_PAGE_HTML


_REVIEW_PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>VRU检测QA审核</title>
<style>
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif; margin: 0; background: #1b1d21; color: #eee; }
  header { position: sticky; top: 0; background: #23262b; padding: 12px 16px; display: flex; gap: 12px; align-items: center; z-index: 10; box-shadow: 0 2px 6px rgba(0,0,0,.4); flex-wrap: wrap; }
  header select, header input, header button { font-size: 14px; padding: 6px 10px; border-radius: 6px; border: 1px solid #444; background: #2f333a; color: #eee; }
  header button { cursor: pointer; }
  header button.pass { background: #2e7d32; border-color: #2e7d32; }
  header button.fail { background: #b71c1c; border-color: #b71c1c; }
  header button#submitBtn { background: #1565c0; border-color: #1565c0; font-weight: bold; }
  header button#submitBtn:disabled { background: #2f333a; border-color: #444; color: #777; cursor: not-allowed; }
  #status { margin-left: auto; color: #9aa; font-size: 13px; }
  /* 默认固定3列，图放大；屏幕窄的时候自动降到2列/1列 */
  #grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; padding: 16px; }
  @media (max-width: 1100px) { #grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 700px) { #grid { grid-template-columns: 1fr; } }
  .card { background: #23262b; border-radius: 8px; overflow: hidden; border: 4px solid transparent; }
  .card.verdict-pass { border-color: #2e7d32; border-style: solid; }
  .card.verdict-fail { border-color: #b71c1c; border-style: solid; }
  .card.pending-pass { border-color: #66bb6a; border-style: dashed; }
  .card.pending-fail { border-color: #ef5350; border-style: dashed; }
  .card img { width: 100%; display: block; background: #000; }
  .card .info { padding: 8px 10px; font-size: 12px; color: #ccc; }
  .card .info .name { font-weight: bold; color: #fff; word-break: break-all; }
  .card .info .pending-tag { color: #ffca28; }
  .card .actions { display: flex; gap: 6px; padding: 0 10px 10px; }
  .card .actions button { flex: 1; padding: 6px; border-radius: 5px; border: 1px solid #444; background: #2f333a; color: #eee; cursor: pointer; font-size: 13px; }
  .card .actions button.active-pass { background: #2e7d32; border-color: #2e7d32; }
  .card .actions button.active-fail { background: #b71c1c; border-color: #b71c1c; }
  .card .img-wrap { position: relative; }
  .card input[type=checkbox] { position: absolute; top: 8px; left: 8px; width: 20px; height: 20px; z-index: 5; }
  .missing-placeholder { aspect-ratio: 16/9; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; background: #14161a; color: #999; font-size: 13px; text-align: center; padding: 12px; }
  .missing-placeholder button { padding: 6px 14px; border-radius: 5px; border: 1px solid #1565c0; background: #1565c0; color: #fff; cursor: pointer; }
</style>
</head>
<body>
<header>
  <label>批次: <select id="dateSelect"></select></label>
  <label>模型: <select id="modelSelect"><option value="">(批次默认)</option></select></label>
  <input id="reviewerInput" placeholder="你的名字/工号" style="width:120px">
  <button id="selectAllBtn">全选</button>
  <button class="pass" id="batchPassBtn">所选标为通过</button>
  <button class="fail" id="batchFailBtn">所选标为不通过</button>
  <button id="submitBtn" disabled>提交更改 (0)</button>
  <span id="status"></span>
</header>
<div id="grid"></div>

<script>
// 设计：点按钮只在本地打标记(pendingVerdict)，不立刻发请求——避免每次点击
// 都要等一次网络往返造成卡顿。只有点"提交更改"才一次性批量发给后端。
//
// 模型对比：切换顶部"模型"下拉框后，按命名规则(raw_f{帧号}_{模型文件名去.pt}_manual.jpg)
// 直接尝试加载对应图，同一帧、换模型，可比。加载失败(该模型没检测过这一帧)
// 显示"点击检测"占位，点了才现场调用/qa/detect跑一次，跑完结果缓存在
// item.compareResults里，同一模型不用重复跑。
let currentItems = [];
let currentModel = '';
let defaultModelFilename = '';  // AERIAL_VRU_MODEL_PATH当前指向的文件名

async function loadDates() {
  const res = await fetch('/qa/batches');
  const data = await res.json();
  const sel = document.getElementById('dateSelect');
  sel.innerHTML = data.dates.map(d => `<option value="${d}">${d}</option>`).join('');
  if (data.dates.length) loadBatch(data.dates[0]);
}

async function loadModels() {
  const res = await fetch('/qa/models');
  const data = await res.json();
  defaultModelFilename = data.default || '';
  const sel = document.getElementById('modelSelect');
  sel.innerHTML = '<option value="">(批次默认)</option>' +
    data.models.map(m => {
      const tag = m === defaultModelFilename ? ' ← 当前AERIAL_VRU_MODEL_PATH' : '';
      return `<option value="${m}">${m}${tag}</option>`;
    }).join('');
}

async function loadBatch(dateStr) {
  document.getElementById('status').textContent = '加载中...';
  const res = await fetch(`/qa/batches/${dateStr}`);
  const data = await res.json();
  currentItems = data.items.map(item => ({...item, pendingVerdict: null, compareResults: {}}));
  renderGrid();
}

function modelStemOf(filename) {
  return filename.replace(/\.pt$/i, '');
}

function getDetectionDisplay(item, idx) {
  // 返回 {url, n_boxes, modelLabel}；批次默认模型直接用manifest里现成的，
  // 对比模式下优先用本次session已经现场检测过的缓存结果，否则按命名规则
  // 猜一个路径去试(试失败会走img的onerror占位逻辑)。
  //
  // 下拉框选中的模型如果正好是当前AERIAL_VRU_MODEL_PATH指向的那个文件，
  // 直接走批次默认这条路径——不能按文件名去猜_manual后缀的文件，因为
  // 批次默认的文件名是按AERIAL_VRU_MODEL_NAME/VERSION(人起的名字)命名的，
  // 跟模型文件名本身不一定一样，猜了也找不到("未检测"是误报)。
  if (!currentModel || currentModel === defaultModelFilename) {
    return {
      url: '/qa/thumbnail?path=' + encodeURIComponent(item.detection_image),
      n_boxes: item.n_boxes,
      modelLabel: `${item.model_name}/${item.model_version}`,
    };
  }
  const cached = item.compareResults[currentModel];
  if (cached) {
    return {
      url: '/qa/thumbnail?path=' + encodeURIComponent(cached.detection_image) + '&t=' + Date.now(),
      n_boxes: cached.n_boxes,
      modelLabel: `${modelStemOf(currentModel)}/manual`,
    };
  }
  const guessPath = `${item.test_dir}/raw_f${item.frame_idx}_${modelStemOf(currentModel)}_manual.jpg`;
  return {
    url: '/qa/thumbnail?path=' + encodeURIComponent(guessPath),
    n_boxes: '-',
    modelLabel: `${modelStemOf(currentModel)}/manual`,
  };
}

function renderGrid() {
  const grid = document.getElementById('grid');
  grid.innerHTML = currentItems.map((item, idx) => {
    const savedVerdict = item.review ? item.review.verdict : null;
    const displayVerdict = item.pendingVerdict || savedVerdict;
    let cardClass = '';
    if (item.pendingVerdict === 'pass') cardClass = 'pending-pass';
    else if (item.pendingVerdict === 'fail') cardClass = 'pending-fail';
    else if (savedVerdict === 'pass') cardClass = 'verdict-pass';
    else if (savedVerdict === 'fail') cardClass = 'verdict-fail';
    const videoName = item.storage_path.split('/').pop();
    const det = getDetectionDisplay(item, idx);
    const pendingTag = item.pendingVerdict ? '<span class="pending-tag"> ● 待提交</span>' : '';
    return `
      <div class="card ${cardClass}" data-idx="${idx}">
        <div class="img-wrap" data-idx="${idx}">
          <input type="checkbox" class="sel-box" data-idx="${idx}">
          <img src="${det.url}" loading="lazy" onerror="handleImgError(${idx})">
        </div>
        <div class="info">
          <div class="name">${videoName}${pendingTag}</div>
          <div>帧${item.frame_idx} · 检测框${det.n_boxes}个 · ${det.modelLabel}</div>
        </div>
        <div class="actions">
          <button class="${displayVerdict === 'pass' ? 'active-pass' : ''}" onclick="markVerdict(${idx}, 'pass')">通过</button>
          <button class="${displayVerdict === 'fail' ? 'active-fail' : ''}" onclick="markVerdict(${idx}, 'fail')">不通过</button>
        </div>
      </div>`;
  }).join('');
  updateStatus();
}

function handleImgError(idx) {
  const wrap = document.querySelector(`.img-wrap[data-idx="${idx}"]`);
  if (!wrap) return;
  wrap.innerHTML = `<div class="missing-placeholder">该模型尚未检测过此帧<br><button onclick="runDetect(${idx})">点击检测</button></div>`;
}

async function runDetect(idx) {
  const item = currentItems[idx];
  const wrap = document.querySelector(`.img-wrap[data-idx="${idx}"]`);
  if (wrap) wrap.innerHTML = `<div class="missing-placeholder">检测中...</div>`;
  try {
    const res = await fetch('/qa/detect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        test_dir: item.test_dir,
        frame_idx: item.frame_idx,
        raw_image: item.raw_image,
        model_filename: currentModel,
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    item.compareResults[currentModel] = {detection_image: data.detection_image, n_boxes: data.n_boxes};
    renderGrid();
  } catch (e) {
    if (wrap) wrap.innerHTML = `<div class="missing-placeholder">检测失败，${e.message}<br><button onclick="runDetect(${idx})">重试</button></div>`;
  }
}

function updateStatus() {
  const reviewed = currentItems.filter(i => i.review).length;
  const pending = currentItems.filter(i => i.pendingVerdict).length;
  document.getElementById('status').textContent = `共${currentItems.length}条，已保存${reviewed}条，待提交${pending}条`;
  const btn = document.getElementById('submitBtn');
  btn.disabled = pending === 0;
  btn.textContent = `提交更改 (${pending})`;
}

function markVerdict(idx, verdict) {
  const item = currentItems[idx];
  const saved = item.review ? item.review.verdict : null;
  // 再点一次同一个值，且跟已保存的一致，就取消pending(等于没改)
  item.pendingVerdict = (item.pendingVerdict === verdict) ? null : verdict;
  if (item.pendingVerdict === saved) item.pendingVerdict = null;
  renderGrid();
}

function getSelectedIndexes() {
  return Array.from(document.querySelectorAll('.sel-box:checked')).map(el => parseInt(el.dataset.idx));
}

function batchMarkVerdict(verdict) {
  const idxs = getSelectedIndexes();
  if (!idxs.length) return;
  idxs.forEach(i => markVerdict(i, verdict));
}

async function submitPending() {
  const pendingItems = currentItems.filter(i => i.pendingVerdict);
  if (!pendingItems.length) return;
  const reviewedBy = document.getElementById('reviewerInput').value || null;
  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = '提交中...';
  await fetch('/qa/submit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      reviewed_by: reviewedBy,
      items: pendingItems.map(item => ({
        test_dir: item.test_dir,
        frame_idx: item.frame_idx,
        model_name: item.model_name,
        model_version: item.model_version,
        verdict: item.pendingVerdict,
      })),
    }),
  });
  pendingItems.forEach(item => {
    item.review = {verdict: item.pendingVerdict};
    item.pendingVerdict = null;
  });
  renderGrid();
}

document.getElementById('dateSelect').addEventListener('change', e => loadBatch(e.target.value));
document.getElementById('modelSelect').addEventListener('change', e => {
  currentModel = e.target.value;
  renderGrid();
});
document.getElementById('batchPassBtn').addEventListener('click', () => batchMarkVerdict('pass'));
document.getElementById('batchFailBtn').addEventListener('click', () => batchMarkVerdict('fail'));
document.getElementById('submitBtn').addEventListener('click', submitPending);
document.getElementById('selectAllBtn').addEventListener('click', () => {
  const boxes = document.querySelectorAll('.sel-box');
  const allChecked = Array.from(boxes).every(b => b.checked);
  boxes.forEach(b => b.checked = !allChecked);
});

loadDates();
loadModels();
</script>
</body>
</html>
"""
