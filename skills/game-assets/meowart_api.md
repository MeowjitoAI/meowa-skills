# MeowArt API 简明文档

常用脚本：

- `skills/meowart_api.py`

这个文档只保留最常用的调用方式，作为快速入口。
更完整的参数、子命令和内部实现，请直接阅读脚本源码。

可用能力概览：

- `credits-balance`：查询当前账号剩余 credit，适合在批量生成前先确认额度是否充足。
- `gemini-generate-content`：通用生成入口（nano banana)，适合自由度较高的文生图, 适合用来生成大尺度的背景，人物等。。
- `pixel-gen-run`：基于模板生成像素图片，适合角色、物件、道具这类固定尺寸的 Sprite；命令会自动提交、轮询并保存结果。某些模板支持批量生成 N 个 Sprite，但是费用不变。
- `self-loop-run`：基于现有图片生成 self-loop 无缝循环图。目前支持横向或纵向无缝拼接，适合用于横版卷轴背景、纵向场景和可重复平铺的纹理。
- `remove-background-run`：对现有图片做去背景处理
  - 像素图片使用 `pixel` 模式，只支持去白色背景。支持任意尺寸输入，最好提前做过 pixelate，且不需要提前缩放到 nano banana尺寸。
  - 普通图片使用 `hd`，支持任意背景色
- `pixelate-run`：把较大的图片重新收敛成更干净的像素风输出，适合在 AI 先生成大图后，将其变为完美像素的 Spite。
- `animate-run`：基于单张角色图生成动作动画，适合做角色待机、跑步、跳跃、弹跳这类短循环动画。

## 1. 鉴权

在使用 MeowArt API 前，先登录 [https://meowart.ai/#/api-keys](https://meowart.ai/#/api-keys)，然后点击左侧的 `Create API Key` 按钮，创建一个 token。

默认使用真实用户 API key：

```http
Authorization: Bearer ma_live_xxxxxxxxxxxxxxxxxxxx
```

推荐先设置环境变量：

```bash
export MEOWART_API_KEY="ma_live_xxxxxxxxxxxxxxxxxxxx"
```

`skills/meowart_api.py` 在未显式传入 `--api-key` 时，会自动读取这个环境变量。

也可以写入当前目录、`skills/` 目录或项目根目录的 `.env`：

```bash
MEOWART_API_KEY="ma_live_xxxxxxxxxxxxxxxxxxxx"
```

推荐优先使用环境变量或 `.env`，尽量不要在每次调用 API 时都显式传入 `--api-key`，因为这样不够安全，容易出现在 shell 历史、日志或截图里。

如果运行时找不到 `MEOWART_API_KEY`，应先提醒用户配置 key；需要时也可以直接帮用户打开 API key 页面：

```bash
open "https://meowart.ai/#/api-keys"
```

这样用户可以立刻前往创建或查看自己的 API key。

## 2. 安装

```bash
pip install requests
python3 skills/meowart_api.py --help
```

## 3. Credits

```bash
python3 skills/meowart_api.py credits-balance
```

## 4. General Image Generation

The most common entry point is `generateContent`:

```bash
python3 skills/meowart_api.py \
  gemini-generate-content \
  --text "Write a one-line description of a cream sofa"
```

If you need to customize the path or request body, check the `gemini-*` subcommands in `skills/meowart_api.py`.

## 5. 像素sprite生成

`pixel-gen` 最常用的入口是 `pixel-gen-run`，它会自动完成提交、轮询和结果下载。

```bash
python3 skills/meowart_api.py \
  pixel-gen-run \
  --template-name "pixel_character_large" \
  --requirement "A fox rogue with twin daggers" \
  --template-config '{"direction":"left"}'
```

常用参数：

- `--template-name`
- `--requirement`
- `--template-config '{}'`
- `--dry-run`
- `--no-wait`
- `--output-dir ./outputs/pixel_gen`

现在脚本也兼容把 `--output-dir` / `--work-dir` 写在子命令后面，下面两种写法都可以：

```bash
python3 skills/meowart_api.py --output-dir ./outputs pixel-gen-run ...
python3 skills/meowart_api.py pixel-gen-run --output-dir ./outputs ...
```

`*-run` 命令在提交成功后，也会立刻打印：

- `planned_output_dir=...`：预计保存目录
- `submitted api_job_id=...`：服务端任务 id

### requirement 的写法建议

这里最容易犯错的一点是：`--requirement` 不等于“最终发给模型的完整 prompt”。

- 模板会先根据自己的默认配置决定一次生成几个对象，例如 `cat_2` 默认是 `8` 个。
- 服务端会结合模板的 `target_count`，把你的 `requirement` 解析并包装成真正的生成 prompt。并对你的 prompt 进行一定的润色或精简。
- 所以对于 `cat`、`cat_2` 等批量模板，`--requirement` 更适合写“这一批要生成什么”，而不是写成“生成一只……白色背景……完整角色……”这种单图式 prompt。

更具体地说：

- 如果模板默认就是批量生成 `8` 个 sprite，那么 `--requirement "猫咪"` 不是“只生成 1 只”，而是会被服务端理解成“生成 8 个猫咪”。
- 如果你想让 8 个 sprite 更有区分度，应该在 `requirement` 里直接描述一批变体，例如：`三花、橘猫、奶牛猫、暹罗、英短、美短、狸花、纯白猫，每一只都带着不同的帽子。`
- 不要在 `requirement` 里重复写模板已经隐含的约束，例如很多 `pixel-gen` 模板本身就默认是白底/透明底，这种情况下再写“白色背景”通常是冗余的，只做内容的描述，一切的其他部分模板内部都会自动化掉。
- 不要把“生成一个角色”的说法机械地套到批量模板上；单体模板和批量模板的 `requirement` 写法应该不同。

推荐示例：

```bash
python3 skills/meowart_api.py \
  pixel-gen-run \
  --template-name "cat_2" \
  --requirement "三花、橘猫、奶牛猫、暹罗、英短、美短、狸花、纯白猫"
```

如果你只是想快速试一下模板是否能跑通，甚至可以更短：

```bash
python3 skills/meowart_api.py \
  pixel-gen-run \
  --template-name "cat_2" \
  --requirement "猫咪"
```

此时服务端仍会按照该模板默认的 `target_count` 去生成一整批结果，而不是只生成 1 张。

如果你只是想先看有哪些模板，可以先执行：

```bash
python3 skills/meowart_api.py pixel-gen-template-info
```

如果模板信息里写了 `supports_direction: true`，那么这个模板支持设置朝向。
设置方式不是单独传 `--direction`，而是放在 `--template-config` 里，例如：

```bash
python3 skills/meowart_api.py \
  pixel-gen-run \
  --template-name "pixel_character_large" \
  --requirement "A fox rogue with twin daggers" \
  --template-config '{"direction":"left"}'
```

可以先用 `pixel-gen-template-info` 查看模板返回的 `directions` 和 `default_direction`，再决定传什么值。

如果你需要模板查询、历史记录、取消任务、单独下载输出等低层能力，再去读：

- `skills/meowart_api.py`

## 6. Remove Background

像素图通常用：

```bash
python3 skills/meowart_api.py \
  remove-background-run \
  --image-file ./pixel_image.png \
  --method pixel
```

高清图使用：
```bash
python3 skills/meowart_api.py \
  remove-background-run \
  --image-file ./hd_image.png \
  --method hd
```



## 7. Pixelate

```bash
python3 skills/meowart_api.py \
  pixelate-run \
  --image-file ./input.png
```

这个命令适合先把较大的 AI 图收敛成更干净的小尺寸像素图，再继续做去背景等处理。

## 8. Self-Loop Image Generation

```bash
python3 skills/meowart_api.py \
  self-loop-run \
  --image-file ./tile.png \
  --direction horizontal
```

`self-loop-run` 现在不再支持 `--requirement`，常用场景里只保留输入图片和循环方向这几个核心参数。

## 9. Animate

```bash
python3 skills/meowart_api.py \
  animate-run \
  --image-file ./sprite.png \
  --prompt "slime bouncing" \
  --is-pixel \
  --output-format webp
```

默认示例只展示像素风动画。`--output-format` 常见可选值有 `webp`、`gif` 和 `spritesheet`：

- `webp`：默认选项，适合大多数网页展示
- `gif`：兼容性更直观，适合简单预览或分享
- `spritesheet`：输出序列帧拼图，适合接入游戏或自行控制播放

动画轮询有一个需要注意的兼容性点：

- `skills/meowart_api.py animate-run` 和 `animate-poll` 现在统一通过 `GET /api/jobs?id=<api_job_id>` 轮询动画状态。
- 如果服务端返回的是单条 job，脚本会直接使用；如果返回的是 `items` 列表，脚本会自动按 `job_id` 过滤出目标任务。
- 如果任务已经成功，脚本会像其他 `*-run` 命令一样把返回结果里的可下载产物拉到本地输出目录。
- 如果服务端暂时只返回 `queued`，`animate-run` 会继续自动轮询直到成功、失败、取消或超时。

## 10. 输出目录

这些 `*-run` 命令默认会在脚本目录下创建：

```bash
./.meow_art/<timestamp>_<command>/
```

通常会保存：

- `meta.json`
- `submit_response.json`
- `job_response.json`
- 下载得到的输出文件

如需自定义目录，可使用 `--work-dir` 或 `--output-dir`。更细的行为差异直接看脚本源码即可。
