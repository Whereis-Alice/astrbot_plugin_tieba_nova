# 贴吧解析说明

贴吧解析使用官方 HTTPS 客户端 JSON 接口：

```text
POST https://tiebac.baidu.com/c/f/pb/page
```

请求携带 `_client_type=2`、`_client_version=12.64.1.1`、`kz`、`pn`、`rn`、`lz` 与 `with_floor` 等参数，按字段名排序后使用公开客户端协议盐 `tiebaclient!!!` 计算 MD5 签名。MD5 仅用于协议兼容。此路径无需额外请求 TBS，也不依赖 protobuf 或网页抓取。

解析器验证帖子 ID 和主楼 post ID，只把 floor=1 的富文本作为主楼。富文本 type 3 提供原图候选，type 5 的 `link` 及 `thread.video_info` 提供签名 MP4 地址。媒体地址限定为官方媒体域名；旧头像域名使用具有有效 TLS 证书的官方别名。

接口回复按真实楼层升序筛选。仅有图片或视频的回复会给出媒体数量摘要，避免将回复附件合入主楼。缺失的真实点赞数保存为未知，真实 0 赞保留。关闭回复获取时使用 `rn=1`，不额外拉取回复。

网络请求设置 30 秒预算，JSON 响应按解压后的字节计量，最大 4 MiB。链接匹配使用 ASCII 范围的忽略大小写，避免 Unicode casefold 将域名截断。媒体下载使用硬上限和公开地址校验，卡片资源另有响应大小限制。

## 核对来源

- 基线为 [Nova v1.21.0](https://github.com/Whereis-Alice/astrbot_plugin_media_parser_nova/commit/6500bbe80f136f549967f272e4e78ce0494b230a)，独立版不依赖它的安装目录。
- LLM 工具参考并复用 [Nova v1.22.1](https://github.com/Whereis-Alice/astrbot_plugin_media_parser_nova/commit/917176e8c8462efc6bfacabf0e717fd820d1f958) 的参数、内容过滤及限额实现，在独立贴吧插件中注册为 `tieba_fetch_content`，不读取主插件配置或运行目录。
- 使用贴吧接口真实响应核对主楼、图文回复、视频签名及删帖错误；单元测试使用可控响应覆盖异常边界。
- [astrbot_plugin_video_analysis 的贴吧解析器](https://github.com/Foolllll-J/astrbot_plugin_video_analysis/blob/19161ad88f851afd69bfd064db1c3103674063a6/modules/tieba/parser.py)仅用于交叉核对接口路径，未直接复制其解析实现。

客户端接口和签名媒体链接都可能随平台更新而变化。当前回复是首屏楼层预览，不声明为热评排行；不支持全帖翻页、楼中楼或需要绕过权限的内容。
