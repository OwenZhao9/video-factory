# 产品图 → 15s TikTok 带货视频（Seedance 2.0）

一条命令：给一张产品图，产出 15 秒竖屏带货视频。
分镜结构复刻实拍参考视频：**举垫开场 → 倒咖啡防水演示 → 一擦即净 → 口播收尾**。

## 🌐 网页版（给不懂代码的人用）

双击 **启动视频工厂.command** → 浏览器自动打开操作页面：
填 API 令牌 → 上传产品图（+可选细节特写）→ 改卖点台词 → 点生成 → 下载视频。

- 令牌客户自己填，费用走填令牌的账户；令牌只存在本机 `apikey.txt`
- 页面实时显示预估创作点消耗和生成进度，完成后可在线预览并下载
- 客户机器要求：macOS 自带 Python3 即可（首次运行如提示装"命令行开发者工具"，点安装）；
  装了 ffmpeg 更好（没有也能出片，只是跳过格式统一）

以下为命令行版说明（开发者用）：

默认走 **NoToken 中转**（[接口文档](https://notokenpro.apifox.cn/)），无需在火山充值开通；
也支持 `--provider ark` 走火山方舟官方。

## 一次性准备（约 3 分钟）

1. **兑换创作点**：登录 [notoken.pro](https://notoken.pro) → 钱包管理 → 用兑换码充值创作点。
2. **创建令牌**：控制台 → 令牌管理 → 添加令牌 → 复制（就是你在其他工具里加平台用的那个令牌）。
3. **粘贴令牌**：打开本目录的 `apikey.txt`，把占位行替换成你的令牌，保存。
   （也可以用环境变量 `NOTOKEN_API_KEY`，优先级更高。注意：`apikey.txt` 含密钥，别发给别人。）
4. 确认本机有 `ffmpeg`（已检测：`/opt/homebrew/bin/ffmpeg` ✅）。

<details>
<summary>备选：火山方舟官方（--provider ark）</summary>

1. [方舟控制台](https://console.volcengine.com/ark) →「开通管理」开通 Doubao-Seedance 2.0
   （需实名 + 余额 > 200 元或资源包）。
2. 「API Key 管理」创建 Key → `export ARK_API_KEY="xxx"`。
3. 运行时加 `--provider ark`。费用：720p 15s ≈ 15 元，1080p ≈ 37 元。
</details>

## 日常使用

```bash
cd "/Users/owenzhao/partTimeA/tiktok视频/seedance-workflow"

# 1. 把产品图放进 input/（白底渲染图效果最好）
# 2. 生成：
python3 generate.py input/product.png
```

等待几分钟，成品在 `output/tiktok_*.mp4`。

## 两种生成模式

| 模式 | 命令 | 说明 |
|------|------|------|
| 单次多镜头（默认） | `python3 generate.py input/product.png` | 一次生成 15s 四镜头，靠 Seedance 2.0 多镜头能力自动切镜 |
| 分段拼接 | `python3 generate.py input/product.png --mode segments` | 3 段 ×5s 分别生成后 ffmpeg 拼接，单段可控性更强，废片可只重跑一段 |

## 常用调整

- **改分镜/文案**：编辑 [prompts.py](prompts.py)（官方六段式结构说明在文件头注释里；
  台词默认英语，发抖音改中文即可）。
- **临时换提示词**：`--prompt "你的完整提示词"`。
- **分辨率**：默认 720p（720×1280）。`--resolution 1080p` 更清晰但贵约 2.5 倍。
- **产品图用法**：默认 `reference_image`（参考图，保产品一致、镜头自由切换）；
  `--image-role first_frame` 则视频从产品图那一帧开始。
- **参考实拍视频的运镜**：`--ref-video input/参考视频.mp4`
  ⚠️ 官方不支持含写实真人脸部的参考素材，可能被拒，被拒就去掉该参数。
- **换模型**：`--model seedance-2.0`（中转文档里两种写法都出现过：
  `doubao-seedance-2.0`（默认）和 `seedance-2.0`，报"模型不存在"就换另一个；
  方舟官方则用 `doubao-seedance-2-0-260128`，省钱试提示词可用
  `--provider ark --model doubao-seedance-2-0-mini-260615`）。

## 产出规格

9:16 竖屏 · 24fps · 精确 15.0s · H.264 + AAC · faststart · 含 AI 配音/音效/BGM
（可直接上传 TikTok/抖音）。

## 费用参考

- **NoToken 中转**：按创作点扣费，控制台「使用日志」可查每笔扣费记录。
  建议先跑一条看扣多少点，再决定批量节奏。
- **方舟官方**（按 token 计费）：720p 15s ≈ 15 元；1080p 15s ≈ 37 元；
  mini 模型 720p ≈ 7.5 元。

## 提示

- 生成的视频 URL **24 小时过期**，脚本已自动下载到本地 `output/`。
- 15s 视频生成通常需要几分钟，脚本会自动轮询等待。
- **创建任务报 5xx 错误时不要盲目重跑**：先去平台控制台确认任务是否已建成，
  避免重复扣费（脚本已内置此保护，只自动重试轮询和下载）。
- 中转模式下产品图会先上传到 notoken.pro 的文件接口再引用（其文档要求的图生视频流程）。
- 生成结果有随机性：同一提示词多跑几条挑最好的发布（带货视频常规做法）。
