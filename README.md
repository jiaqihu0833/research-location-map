# Research Location Map · 科研区位图

面向地理学、城乡规划学论文的 Codex skill，使用真实空间数据制作研究区区位图。

**状态：第一版，仅完成静态审查，尚未执行真实数据出图测试。**

## 能力

- 公开数据、用户上传数据和混合数据工作流。
- 行政区位、地形水系、交通与样点三类图。
- 两级或三级定位小图、区域主图和研究区细节图。
- CRS与数据年份核查、图例、比例尺、真北和定位框。
- 本地 Python 绘图辅助程序与 PNG/SVG/PDF 输出设计。
- 可选 QGIS 可编辑工程工作流；不把 QGIS MCP 设为强制依赖。

## 开始使用

入口为 [SKILL.md](SKILL.md)。将本目录作为 `research-location-map` 技能目录使用，或直接让 Codex 读取该文件。按当前客户端的技能配置方式启用；本仓库不自动安装依赖或更改配置。

请求示例：

> 使用 research-location-map，基于我提供的研究区边界和样点制作两级中文论文区位图。先核对坐标、数据年份、图件尺寸和执行权限。

复制 [配置模板](assets/request-template.json) 到独立任务目录，填写真实文件、CRS、范围与来源。模板中的空值代表待确认信息，不能直接运行。

## 文件

- `SKILL.md`：技能入口。
- `agents/openai.yaml`：Codex 界面元数据。
- `assets/request-template.json`：制图请求模板。
- `scripts/render_location_map.py`：读取已准备好的本地数据，不负责联网下载或地名解析。
- `references/`：数据、投影、布局、配置、QGIS流程和验收规范。

## 执行边界

不猜测坐标系，不用生成式图片替代真实地理要素，不覆盖原始数据。安装依赖、下载数据、运行程序及上传文件应遵循当前用户授权。

核心路线使用 GeoPandas、Cartopy、Matplotlib、pyproj；地形和Excel输入分别需要适用的Rasterio和表格读取引擎。实际版本兼容性尚待验证。脚本限常规两级/三级投影地图，不支持跨日期变更线、自动标注避让或GCJ-02/BD-09静默转换。

详见 [验证状态](references/development-status.md) 和 [验收场景](references/quality-and-acceptance.md)。
