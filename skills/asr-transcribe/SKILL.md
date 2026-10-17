---
name: asr-transcribe
description: 将面试音视频（mp3/wav/mp4/m4a）转写为文字稿，供人才筛选引擎装配上下文。当前为 mock 占位引擎，预留真实 ASR（本地 Whisper / 外部 API）接入点。
version: 0.1.0
type: skill
---

# asr-transcribe · 音视频转写独立 Skill

## 用途
人才筛选流水线中，把线上面试的音视频转成文字稿，作为「上下文」喂给多 Agent 评判引擎
（正方/反方/裁判/评委）。属于 Layer A 的上下文装配环节。

## 调用方式
```bash
python transcribe.py <输入音视频路径> [--engine mock|whisper|api]
```
输出 JSON：`{"text": "...", "engine": "mock", "duration_sec": 0, "source": "<path>"}`

## 引擎实现状态
- `mock`  ：当前默认。返回占位文字稿，保证端到端流程可跑通（无需密钥）。
- `whisper`：预留。后续接入 faster-whisper 本地离线转写。
- `api`    ：预留。后续接入外部 ASR（如讯飞/阿里云），需配置密钥。

## 接入真实引擎（TODO）
1. whisper：安装 faster-whisper，调用 `WhisperModel.transcribe()`。
2. api    ：在 transcribe.py 中实现对应厂商 HTTP 调用，密钥走环境变量。
3. 引擎选择可由环境变量 `ASR_ENGINE` 控制，默认 `mock`。

## 在评判引擎中如何被使用
`engine/agents.py` 的 `build_context()` 预留 `interview_transcript` 字段；
真实接入时，先用本 skill 把音视频转写，再写入该字段即可，无需改动辩论/打分逻辑。
