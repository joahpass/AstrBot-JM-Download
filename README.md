# astrbot_plugin_jmcomic_reader

AstrBot 插件，内嵌 JMComicReaderProject 的核心搜索、下载和本地管理能力。

上传到 AstrBot WebUI 后即可使用，不需要单独启动 JMComicReaderProject Flask 服务。

## 安装

在 AstrBot WebUI 上传本插件 zip，或将插件目录放入：

```text
AstrBot/data/plugins/astrbot_plugin_jmcomic_reader
```

AstrBot 会根据 `requirements.txt` 安装依赖。

## 数据目录

插件会把下载内容、缓存、数据库和 JM 配置写入 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_jmcomic_reader/
```

漫画下载文件会单独放在配置的下载文件夹中，默认是：

```text
data/plugin_data/astrbot_plugin_jmcomic_reader/ComicDownloads/
```

历史版本使用的 `DownloadedComics/` 会在启动时自动迁移到新的下载文件夹。

插件更新不会覆盖这个目录。

## 配置

- `max_search_results`：搜索和列表最多展示数量。
- `allow_download`：是否允许通过聊天命令启动下载，默认关闭。
- `download_poll_seconds`：启动下载后自动等待并回报一次进度的秒数。
- `download_dir_name`：漫画下载文件夹名称或绝对路径，默认 `ComicDownloads`。
- `user_whitelist`：个人白名单，填写用户 QQ 号。
- `group_whitelist`：群聊白名单，填写群号。
- `render_text_as_image`：是否把详情和下载提示渲染为图片卡片，默认开启。
- `render_cover_enabled`：图片卡片是否附带漫画封面，默认开启。
- `auto_delete_enabled`：是否启用定时删除已下载漫画文件，默认关闭。
- `auto_delete_after_hours`：下载目录保留小时数，默认 24。
- `auto_delete_interval_minutes`：后台检查间隔分钟数，默认 30。

下载功能默认关闭。确认你有权下载/缓存相关内容后，再在 WebUI 中开启：

```text
allow_download = true
```

如需限制可用范围，可配置白名单：

```text
user_whitelist = ["123456789"]
group_whitelist = ["987654321"]
```

白名单规则：

- 两个白名单都为空时不限制使用。
- 个人白名单优先级最高；命中个人白名单的用户可以在任意群聊或私聊使用。
- 未命中个人白名单时，群聊命令需要当前群号命中群聊白名单。
- 配置了白名单但个人和群聊都未命中时，插件会拒绝执行。

如需自动清理本地下载文件，可开启：

```text
auto_delete_enabled = true
auto_delete_after_hours = 24
auto_delete_interval_minutes = 30
```

自动删除会按下载目录的修改时间判断是否过期。删除时会同时删除本地漫画目录和数据库记录，不会撤回已经上传到聊天对话里的文件。

## 图片卡片输出

默认情况下，下面两类输出会渲染为图片卡片：

```text
/jm <JM号>
/jm 下 <JM号>
```

如果插件成功获取到漫画封面，会把封面拼在图片左侧；封面获取失败时仍会发送纯文本渲染图。

如需恢复纯文本输出，可在配置中关闭：

```text
render_text_as_image = false
```

如需保留图片卡片但不显示封面，可关闭：

```text
render_cover_enabled = false
```

## 日常命令

```text
/jm
/jm 搜 <关键词> [页码]
/jm <JM号>
/jm 下 <JM号>
/jm 进 <JM号或download_id>
/jm 看 <JM号>
/jm 列
/jm 状态
```

## 下载和上传

使用下面命令启动下载：

```text
/jm 下 <JM号>
```

插件会在触发命令的当前对话中返回下载任务信息：

```text
download_id: <JM号_时间戳>
查进度: /jm 进 <download_id>
```

下载完成后，插件会自动把生成的 PDF 文件上传回触发下载的同一个对话，例如群聊或私聊。

如果该漫画之前已经下载过，再次执行 `/jm 下 <JM号>` 时不会重复下载，会直接尝试把本地已存在的 PDF 上传到当前对话。

查询进度：

```text
/jm 进 <download_id>
/jm 进 <JM号>
```

如果 AstrBot 重启导致内存进度丢失，插件会回查本地下载记录；只要文件已经落库，进度会显示为完成。

`/jm 看 <JM号>` 只返回本地阅读信息和文件路径，不负责上传文件。需要上传到当前对话时，请使用 `/jm 下 <JM号>`。

示例：

```text
/jm 搜 关键词
/jm 搜 关键词 2
/jm 123456
/jm 下 123456
/jm 进 123456_20260529235959
/jm 看 123456
/jm 列
/jm 状态
```

## 删除命令

删除本地漫画使用管理员命令：

```text
/jm_delete <JM号>
```

`/jm 删 <JM号>` 只会提示改用管理员命令，不会直接删除。

## 英文兼容命令

```text
/jm_help
/jm_status
/jm_search <关键词> [页码]
/jm_info <JM号>
/jm_download <JM号>
/jm_progress <download_id>
/jm_list
/jm_read <JM号>
/jm_delete <JM号>
```

## 注意

- 本插件不内置漫画资源。
- 本插件仅用于管理你有权下载、缓存、阅读的内容。
- 如果服务器网络无法访问 JM 相关域名，搜索/下载会返回网络错误；这需要处理服务器网络环境。
