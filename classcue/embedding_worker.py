"""Isolated, offline Qwen embedding inference; stdout is a JSON protocol."""

import argparse
import base64
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
                      SAFETENSORS_LOAD_METHOD="pread")
    import torch
    import torch.nn.functional as F
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(max(1, min(8, (os.cpu_count() or 2) - 2)))
    if args.device == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用，请在课程检索设置中选择 CPU")
        torch.zeros(1, device="cuda")
    tokenizer = AutoTokenizer.from_pretrained(args.model, padding_side="left", local_files_only=True)
    model = AutoModel.from_pretrained(
        args.model, local_files_only=True, trust_remote_code=False,
        torch_dtype=torch.float16 if args.device == "cuda" else torch.float32,
        attn_implementation="sdpa",
    ).to(args.device).eval()
    print(json.dumps({"event": "ready", "device": args.device}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            texts = request["texts"]
            if not isinstance(texts, list) or not 1 <= len(texts) <= 16 or any(
                not isinstance(text, str) or not text or len(text) > 4000 for text in texts
            ):
                raise ValueError("嵌入输入无效")
            if request.get("query"):
                texts = ["Instruct: Given a question about a course, retrieve relevant lecture "
                         "passages that answer the question\nQuery: " + text for text in texts]
            inputs = tokenizer(texts, padding=True, truncation=False, return_tensors="pt")
            if inputs.input_ids.shape[1] > 2048:
                raise ValueError("字幕片段超过 2048 tokens，请缩短片段后重建索引")
            inputs = inputs.to(args.device)
            with torch.inference_mode():
                hidden = model(**inputs).last_hidden_state
                if (inputs.attention_mask[:, -1].sum() == len(texts)).item():
                    pooled = hidden[:, -1]
                else:
                    last = inputs.attention_mask.sum(dim=1) - 1
                    pooled = hidden[torch.arange(len(texts), device=hidden.device), last]
                vectors = F.normalize(pooled.float(), p=2, dim=1).cpu().numpy().astype("<f4")
            print(json.dumps({"event": "result", "count": len(texts),
                              "dimensions": int(vectors.shape[1]),
                              "vectors": base64.b64encode(vectors.tobytes()).decode("ascii")}), flush=True)
            del inputs, hidden, pooled, vectors
        except Exception as error:
            print(json.dumps({"event": "error", "message": str(error)}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
