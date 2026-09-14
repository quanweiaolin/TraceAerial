"""
VRU检测QA人工审核：给人看抽帧+检测叠加图，判断这一帧检测得对不对。

存储沿用之前定的方案(先json，不进库，见对话记录)：
  - 批次清单(哪一天跑了哪些) 在 services/vru_detection._write_batch_manifest 写
  - review结果(通过/不通过) 就近写在各自视频的 Test/ 目录下，
    见 services/vru_detection.write_review/read_review

这个路由只做：读清单+读review状态拼给前端、提供图片文件、接收review提交。
不碰数据库(候选筛选/跑检测那部分在 app/main.py 的 /qa/run-batch 里)。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.services.vru_detection import (
    PROCESSING_ROOT,
    list_batch_dates,
    read_batch_manifest,
    read_review,
    write_review,
)

router = APIRouter(prefix="/qa", tags=["qa_review"])


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
    """按绝对路径读图片文件返回，仅限Aerial_Processing_Data这个根目录下的文件，
    防止被拿来当成任意文件读取接口。"""
    p = Path(path)
    try:
        p.relative_to(PROCESSING_ROOT)
    except ValueError:
        raise HTTPException(403, "path not under allowed root")
    if not p.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(p)


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


@router.get("/review", response_class=HTMLResponse)
async def review_page():
    return _REVIEW_PAGE_HTML


_REVIEW_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>VRU检测QA审核</title>
<style>
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif; margin: 0; background: #1b1d21; color: #eee; }
  header { position: sticky; top: 0; background: #23262b; padding: 12px 16px; display: flex; gap: 12px; align-items: center; z-index: 10; box-shadow: 0 2px 6px rgba(0,0,0,.4); }
  header select, header input, header button { font-size: 14px; padding: 6px 10px; border-radius: 6px; border: 1px solid #444; background: #2f333a; color: #eee; }
  header button { cursor: pointer; }
  header button.pass { background: #2e7d32; border-color: #2e7d32; }
  header button.fail { background: #b71c1c; border-color: #b71c1c; }
  #status { margin-left: auto; color: #9aa; font-size: 13px; }
  #grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; padding: 16px; }
  .card { background: #23262b; border-radius: 8px; overflow: hidden; border: 3px solid transparent; }
  .card.verdict-pass { border-color: #2e7d32; }
  .card.verdict-fail { border-color: #b71c1c; }
  .card img { width: 100%; display: block; background: #000; }
  .card .info { padding: 8px 10px; font-size: 12px; color: #ccc; }
  .card .info .name { font-weight: bold; color: #fff; word-break: break-all; }
  .card .actions { display: flex; gap: 6px; padding: 0 10px 10px; }
  .card .actions button { flex: 1; padding: 5px; border-radius: 5px; border: 1px solid #444; background: #2f333a; color: #eee; cursor: pointer; }
  .card .actions button.active-pass { background: #2e7d32; border-color: #2e7d32; }
  .card .actions button.active-fail { background: #b71c1c; border-color: #b71c1c; }
  .card label.select { position: relative; display: block; }
  .card input[type=checkbox] { position: absolute; top: 8px; left: 8px; width: 18px; height: 18px; z-index: 5; }
</style>
</head>
<body>
<header>
  <label>批次: <select id="dateSelect"></select></label>
  <input id="reviewerInput" placeholder="你的名字/工号" style="width:120px">
  <button id="selectAllBtn">全选</button>
  <button class="pass" id="batchPassBtn">所选标为通过</button>
  <button class="fail" id="batchFailBtn">所选标为不通过</button>
  <span id="status"></span>
</header>
<div id="grid"></div>

<script>
let currentItems = [];

async function loadDates() {
  const res = await fetch('/qa/batches');
  const data = await res.json();
  const sel = document.getElementById('dateSelect');
  sel.innerHTML = data.dates.map(d => `<option value="${d}">${d}</option>`).join('');
  if (data.dates.length) loadBatch(data.dates[0]);
}

async function loadBatch(dateStr) {
  document.getElementById('status').textContent = '加载中...';
  const res = await fetch(`/qa/batches/${dateStr}`);
  const data = await res.json();
  currentItems = data.items;
  renderGrid();
}

function itemKey(item) {
  return `${item.test_dir}|${item.frame_idx}|${item.model_name}|${item.model_version}`;
}

function renderGrid() {
  const grid = document.getElementById('grid');
  grid.innerHTML = currentItems.map((item, idx) => {
    const verdict = item.review ? item.review.verdict : null;
    const cardClass = verdict === 'pass' ? 'verdict-pass' : verdict === 'fail' ? 'verdict-fail' : '';
    const videoName = item.storage_path.split('/').pop();
    const imgUrl = '/qa/image?path=' + encodeURIComponent(item.detection_image);
    return `
      <div class="card ${cardClass}" data-idx="${idx}">
        <label class="select">
          <input type="checkbox" class="sel-box" data-idx="${idx}">
          <img src="${imgUrl}" loading="lazy">
        </label>
        <div class="info">
          <div class="name">${videoName}</div>
          <div>帧${item.frame_idx} · 检测框${item.n_boxes}个 · ${item.model_name}/${item.model_version}</div>
        </div>
        <div class="actions">
          <button class="${verdict === 'pass' ? 'active-pass' : ''}" onclick="setVerdict(${idx}, 'pass')">通过</button>
          <button class="${verdict === 'fail' ? 'active-fail' : ''}" onclick="setVerdict(${idx}, 'fail')">不通过</button>
        </div>
      </div>`;
  }).join('');
  updateStatus();
}

function updateStatus() {
  const reviewed = currentItems.filter(i => i.review).length;
  document.getElementById('status').textContent = `共${currentItems.length}条，已标注${reviewed}条`;
}

async function submitVerdicts(itemsWithVerdict) {
  const reviewedBy = document.getElementById('reviewerInput').value || null;
  await fetch('/qa/submit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      reviewed_by: reviewedBy,
      items: itemsWithVerdict.map(({item, verdict}) => ({
        test_dir: item.test_dir,
        frame_idx: item.frame_idx,
        model_name: item.model_name,
        model_version: item.model_version,
        verdict,
      })),
    }),
  });
}

async function setVerdict(idx, verdict) {
  const item = currentItems[idx];
  await submitVerdicts([{item, verdict}]);
  item.review = {verdict};
  renderGrid();
}

function getSelectedIndexes() {
  return Array.from(document.querySelectorAll('.sel-box:checked')).map(el => parseInt(el.dataset.idx));
}

async function batchSetVerdict(verdict) {
  const idxs = getSelectedIndexes();
  if (!idxs.length) return;
  const pairs = idxs.map(i => ({item: currentItems[i], verdict}));
  await submitVerdicts(pairs);
  idxs.forEach(i => currentItems[i].review = {verdict});
  renderGrid();
}

document.getElementById('dateSelect').addEventListener('change', e => loadBatch(e.target.value));
document.getElementById('batchPassBtn').addEventListener('click', () => batchSetVerdict('pass'));
document.getElementById('batchFailBtn').addEventListener('click', () => batchSetVerdict('fail'));
document.getElementById('selectAllBtn').addEventListener('click', () => {
  const boxes = document.querySelectorAll('.sel-box');
  const allChecked = Array.from(boxes).every(b => b.checked);
  boxes.forEach(b => b.checked = !allChecked);
});

loadDates();
</script>
</body>
</html>
"""
