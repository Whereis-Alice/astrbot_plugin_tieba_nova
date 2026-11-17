# 来源与许可

本插件基于 Whereis-Alice 的 [astrbot_plugin_media_parser_nova](https://github.com/Whereis-Alice/astrbot_plugin_media_parser_nova) `v1.21.0`（提交 `6500bbe80f136f549967f272e4e78ce0494b230a`）提取、裁剪并修改，保留 GNU Affero General Public License v3.0。

LLM 内容抓取工具另复用 Nova `v1.22.1`（提交 `917176e8c8462efc6bfacabf0e717fd820d1f958`）中的工具参数、取数生命周期与结构化内容过滤，修改后在本插件内独立运行，同样遵循主体 AGPL-3.0。

卡片渲染及相关适配层沿用 Nova 中的实现和来源说明：

- [nonebot-plugin-parser](https://github.com/maoxig/nonebot-plugin-parser)：CommonRenderer 参考，附带的 Apache-2.0 许可见 `LICENSES/Apache-2.0.txt`。
- [astrbot_plugin_rika_share](https://github.com/iris1598/astrbot_plugin_rika_share)：卡片及适配参考，MIT 许可见 `LICENSES/rika-share-MIT.txt`。

本次新增的独立入口、配置、缓存隔离与贴吧适配修改遵循主体 AGPL-3.0。完整可安装源码包含在本仓库中。贴吧 JSON 接口独立实现，参考仓库仅用于交叉核对，具体来源见 [解析说明](docs/implementation.md)。
