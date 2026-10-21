## 引用修正任务

这里只修正已提取候选的字幕引用，不重新提取考点。
original_candidate_json 是原候选；evidence_errors_json 明确指出引文、编号和范围的不对应之处。
backend_matching_json 是后端定位失败或存在歧义的诊断，不是新增的教师证据。

只允许修改 evidence_quote、evidence_sentence_ids、start_sentence_id、end_sentence_id。
level、signal_type、title、reason 必须逐字保持原值；必须返回恰好一个对应候选，不删除、不新增、不换成其他知识对象。
只使用本次提供的同一段字幕。复制实际原文，不改写、不遗漏否定词、数字或公式。
优先引用体现原考点信号的连续原文；不要把远隔的片段拼接后遗漏改变含义的中间上下文。
编号按实际原文位置填写，起止范围同时覆盖信号和知识对象。
如果附有 invalid_output_json 和 format_errors_json，先按具体错误修正上轮 JSON 格式，同时保持已合法字段和候选数量不变。
只返回符合上述考点规则的 {"candidates":[...]} JSON，不要 Markdown。
如果无法确认原候选的真实证据或唯一位置，返回 {"repair_failed":true}，保留给人工审查，不能用 {"candidates":[]} 隐藏失败。
