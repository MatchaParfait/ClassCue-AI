"""Create local, side-by-side audio review artifacts from benchmark plans."""

import argparse
import csv
import html
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.compare_asr import _stamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--context-seconds", type=float, default=4)
    args = parser.parse_args()
    import numpy as np
    report = args.report.resolve()
    meta = json.loads((report / "boundary_timings.json").read_text(encoding="utf-8"))
    output = report / "review"
    output.mkdir(parents=True, exist_ok=True)
    modes = {}
    rows = []
    with wave.open(str(report / "timeline.wav"), "rb") as source:
        rate = source.getframerate()
        total = source.getnframes()
        for mode in ("energy", "firered"):
            plan = json.loads((report / f"{mode}_full_plan.json").read_text(encoding="utf-8"))
            entries = []
            for index, span in enumerate(plan["spans"][:-1], 1):
                cut = span["end_sample"]
                start = max(0, cut - round(args.context_seconds * rate))
                end = min(total, cut + round(args.context_seconds * rate))
                source.setpos(start)
                pcm = source.readframes(end - start)
                name = f"{mode}-{index:03d}.wav"
                with wave.open(str(output / name), "wb") as clip:
                    clip.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                    clip.writeframes(pcm)
                audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
                bins = np.array_split(audio, min(320, len(audio)))
                envelope = [[round(float(b.min()), 4), round(float(b.max()), 4)] for b in bins]
                row = {
                    "mode": mode, "index": index,
                    "cut_seconds": cut / rate, "cut_time": _stamp(round(cut / rate * 1000)),
                    "clip_start_seconds": start / rate, "clip_end_seconds": end / rate,
                    "cut_in_clip_seconds": (cut - start) / rate, "reason": span["reason"],
                    "audio_file": name, "review": "pending",
                }
                rows.append(row)
                entries.append({**row, "envelope": envelope})
            modes[mode] = entries
    with (output / "boundary_review.csv").open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    data = json.dumps(modes, ensure_ascii=True).replace("</", "<\\/")
    source_label = html.escape(Path(meta["video"]).name)
    template = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Qwen Boundary Review</title>
<style>
*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;background:#fafafa;color:#222}
header{padding:18px 24px;border-bottom:1px solid #ccc;background:#fff}
h1{font-size:21px;margin:0 0 6px}p{margin:0;color:#666;overflow-wrap:anywhere}
.toolbar{display:flex;gap:12px;align-items:center;padding:14px 24px}
button,select{font:inherit;padding:6px 10px;border:1px solid #aaa;border-radius:4px;background:#fff}
main{padding:0 24px 24px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px;border-top:1px solid #ccc;padding:14px 0}
.item{min-width:0}.title{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
.mode{font-weight:600}.time{font-family:monospace;font-size:15px}.reason{color:#666}
canvas{display:block;width:100%;height:52px;background:#f1f3f4}
audio{width:100%;height:36px;margin:8px 0}.footer{display:flex;align-items:center;justify-content:space-between;gap:8px}
select{max-width:100%}.range{color:#666;font-size:12px}
@media(max-width:640px){header{padding:16px}.toolbar{padding:12px 16px}main{padding:0 16px}.pair{grid-template-columns:1fr;gap:16px}}
</style>
<header><h1>Qwen 边界审核</h1><p>__SOURCE__</p></header>
<div class="toolbar"><button id="export">导出审核 CSV</button><span id="count"></span></div><main id="rows"></main>
<script>
const data=__DATA__;
const storageKey="qwen-boundary-review:"+document.location.pathname;
let saved={};try{saved=JSON.parse(localStorage.getItem(storageKey)||"{}")}catch{}
const options=[["pending","待审"],["pause","停顿内"],["speech","切进发音"],["uncertain","不确定"]];
const all=Object.values(data).flat();
function updateCount(){document.querySelector("#count").textContent=
  `${all.filter(e=>(saved[e.mode+":"+e.index]||"pending")!=="pending").length} / ${all.length}`}
function draw(canvas,e,time){
  const width=canvas.clientWidth, height=canvas.clientHeight, dpr=devicePixelRatio||1;
  canvas.width=width*dpr;canvas.height=height*dpr;
  const ctx=canvas.getContext("2d");ctx.scale(dpr,dpr);
  ctx.strokeStyle="#60736c";ctx.beginPath();
  e.envelope.forEach((a,i)=>{const x=i/(e.envelope.length-1)*width;
    ctx.moveTo(x,height/2-a[0]*height*.46);ctx.lineTo(x,height/2-a[1]*height*.46)});ctx.stroke();
  const duration=e.clip_end_seconds-e.clip_start_seconds;
  ctx.strokeStyle="#c43636";ctx.lineWidth=2;const cut=e.cut_in_clip_seconds/duration*width;
  ctx.beginPath();ctx.moveTo(cut,0);ctx.lineTo(cut,height);ctx.stroke();
  if(time!==undefined){ctx.strokeStyle="#176e9a";ctx.lineWidth=1;const x=time/duration*width;
    ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,height);ctx.stroke()}
}
for(let i=0;i<Math.max(...Object.values(data).map(a=>a.length));i++){
 const pair=document.createElement("section");pair.className="pair";
 for(const mode of ["energy","firered"]){
  const e=data[mode]?.[i];const item=document.createElement("div");item.className="item";
  if(!e){pair.append(item);continue}
  const title=document.createElement("div");title.className="title";
  const reason=e.reason==="energy-fallback"?"能量回退":e.reason==="firered"?"VAD 候选":"低能量";
  title.innerHTML=`<span class="mode">${mode==="energy"?"能量":"FireRed"} #${e.index}</span>
    <span class="time">${e.cut_time}</span><span class="reason">${reason}</span>`;
  const canvas=document.createElement("canvas");
  const audio=document.createElement("audio");audio.controls=true;audio.preload="metadata";audio.src=e.audio_file;
  const footer=document.createElement("div");footer.className="footer";
  const range=document.createElement("span");range.className="range";
  range.textContent=`${e.clip_start_seconds.toFixed(3)}–${e.clip_end_seconds.toFixed(3)} s`;
  const select=document.createElement("select");select.setAttribute("aria-label",`${mode} ${e.index} 审核结果`);
  for(const [value,label] of options){const option=document.createElement("option");option.value=value;option.textContent=label;select.append(option)}
  select.value=saved[mode+":"+e.index]||"pending";
  select.onchange=()=>{saved[mode+":"+e.index]=select.value;
    try{localStorage.setItem(storageKey,JSON.stringify(saved))}catch{}updateCount()};
  footer.append(range,select);item.append(title,canvas,audio,footer);pair.append(item);
  new ResizeObserver(()=>draw(canvas,e,audio.currentTime)).observe(canvas);
  audio.addEventListener("timeupdate",()=>draw(canvas,e,audio.currentTime));
 }
 document.querySelector("#rows").append(pair);
}
updateCount();
document.querySelector("#export").onclick=()=>{
 const cols=["mode","index","cut_seconds","cut_time","reason","review"];
 const lines=[cols.join(","),...all.map(e=>cols.map(k=>k==="review"?(saved[e.mode+":"+e.index]||"pending"):e[k]).join(","))];
 const link=document.createElement("a");link.href=URL.createObjectURL(new Blob(["\\ufeff"+lines.join("\\n")],{type:"text/csv;charset=utf-8"}));
 link.download="boundary_review.csv";link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);
};
</script></html>"""
    (output / "index.html").write_text(
        template.replace("__SOURCE__", source_label).replace("__DATA__", data),
        encoding="utf-8",
    )
    print(f"Review ready: {output / 'index.html'}; {len(rows)} audio boundaries")


if __name__ == "__main__":
    main()
