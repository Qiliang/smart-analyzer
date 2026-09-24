# 指标定义

### STT定稿
当前只计算火山STT定稿延迟。
定稿延迟: 下游 VADUserStoppedSpeakingFrame → 下游 VolcengineSTT 的 TranscriptionWithSpeakerFrame。
只取 direction=d。定稿以帧类型为准，不看 payload.finalized。

配对：开口帧清掉上一声停声；停声只在尚未被本指标终点消费时写入锚点。终点有锚点则记 lag 并锁住，直到下一次开口。终点没有锚点则跳过，不回退到末次 interim 或 UserStoppedSpeakingFrame。定稿已经用掉锚点之后才到的停声，不能再配下一句；当时就没有锚点而跳过的，之后到来的停声仍可配更晚的定稿。

以下不计入：整段没有停声帧；有开口但停声在定稿之后才到；停声之后又开口、新的停声还没到。

### Agent首字
MPAAS_AGENT MetricsFrame.value; 

### TTS 聚合
LLMTextFrame → AggregatedTextFrame; 

### TTS 首音
on_tts_first_audio.ttfb 或 TTS MetricsFrame.ttfb; 

### 听到声音

下游 VADUserStoppedSpeakingFrame → 下游 BotStartedSpeakingFrame。
与 STT 定稿共用同一套锚点规则，但两条指标各自消费，一句定稿不会吃掉听到声音的起点。欢迎语出声时还没有停声锚点，不计入。含垫词。缺停声或续说作废不计入。