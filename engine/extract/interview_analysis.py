"""问答配对（extract 层）。

在"已归属的段"之上，把面试/答辩拆成一条条「问题-回答」对，并四分类：
- asked_answered：问了且紧邻有答辩方回答；
- asked_dodged：问了但到下一问之前没有答辩方回答；
- not_asked：问题清单里有、但全程没人问（如实暴露覆盖缺口）；
- ad_hoc：现场即兴追问（未命中问题清单）。

领域无关：只是一组「提问方段 → 其后直到下一提问方段之间的答辩方段」的配对。
"""
from engine.extract.diarization import similarity


def pair_qa(segments, question_list):
    """segments：diarization.attribute 的输出（含 speaker/asker）。
    question_list：{"questions":[...]} 或 [q,...]，可空。
    """
    questions = _questions(question_list)
    has_list = bool(questions)
    speakers = [s for s in segments if s.get("speaker") != "unassigned"]
    interviewers = [s for s in speakers if s["speaker"] == "interviewer"]
    consumed = set()

    pairs = []
    matched_q = set()
    if questions:
        # 问题清单驱动：每题找最贴合的、未被占用的提问方段配对
        for qi, q in enumerate(questions):
            qt = q["q"] if isinstance(q, dict) else q
            best_seg, best_sim = None, 0.5
            for s in interviewers:
                if id(s) in consumed:
                    continue
                sim = similarity(s["text"], qt)
                if sim > best_sim:
                    best_sim, best_seg = sim, s
            if best_seg is None:
                pairs.append(_pair(qt, None, [], "", "not_asked",
                                   attribution="面试官（按清单判定）"))
                continue
            consumed.add(id(best_seg))
            matched_q.add(qi)
            answer_idx, answer = _answer_after(speakers, best_seg)
            pairs.append(_pair(qt, best_seg, answer_idx, answer,
                               "asked_answered" if answer_idx else "asked_dodged"))
    # 未被清单占用的提问方段 = 现场即兴追问
    for s in interviewers:
        if id(s) in consumed:
            continue
        answer_idx, answer = _answer_after(speakers, s)
        pairs.append(_pair(s["text"], s, answer_idx, answer, "ad_hoc"))

    confirmed = sum(1 for s in segments if s.get("speaker") != "unassigned")
    a_a = sum(1 for p in pairs if p["status"] == "asked_answered")
    a_d = sum(1 for p in pairs if p["status"] == "asked_dodged")
    n_a = sum(1 for p in pairs if p["status"] == "not_asked")
    ad = sum(1 for p in pairs if p["status"] == "ad_hoc")
    return {
        "pairs": pairs,
        "stats": {
            "questions": len(questions),
            "asked_answered": a_a,
            "asked_dodged": a_d,
            "not_asked": n_a,
            "ad_hoc": ad,
            "has_question_list": has_list,
            "segments_confirmed": confirmed,
            "segments_total": len(segments),
        },
    }


def _questions(qlist):
    if qlist is None:
        return []
    if isinstance(qlist, dict):
        qlist = qlist.get("questions", [])
    return list(qlist or [])


def _find_q(text, questions):
    best, best_i = 0.6, None
    for i, q in enumerate(questions):
        qt = q["q"] if isinstance(q, dict) else q
        sim = similarity(text, qt)
        if sim >= best:
            best, best_i = sim, i
    return best_i


def _answer_after(speakers, question_seg):
    idxs, texts = [], []
    started = False
    for s in speakers:
        if s is question_seg:
            started = True
            continue
        if not started:
            continue
        if s["speaker"] == "interviewer":
            break
        idxs.append(s["idx"])
        texts.append(s["text"])
    return idxs, "\n".join(texts)


def _pair(question, q_seg, answer_idx, answer, status, ad_hoc=False, attribution=""):
    return {
        "question": question,
        "asker": q_seg.get("asker", "") if q_seg else "",
        "question_idx": q_seg["idx"] if q_seg else None,
        "answer_idx": answer_idx,
        "answer": answer,
        "status": status if not ad_hoc else "ad_hoc",
        "match": similarity(question, answer) if answer else 0.0,
        "attribution": attribution,
    }
