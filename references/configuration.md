# 配置契约与操作边界

`assets/request-template.json` 是输入表单，不包含虚构地点、边界或样点。复制到新任务目录再填写。未解决的 `null` 不可拿去正式出图。相对输入与输出路径均相对请求 JSON 所在目录，不相对当前终端目录或 skill 目录。

## 顶层字段

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 固定 `1.0` |
| `study_area.name` | 地名及上级区域；说明是否行政区、调查区或规划范围 |
| `study_area.boundary_layer` | 已确认研究区多边形图层的 ID，必须实际存在 |
| `study_area.expected_bbox_wgs84` | 可选 `[west,south,east,north]`，用于发现范围异常；不是研究区边界的替代物 |
| `data_mode` | `public`、`user` 或 `mixed`；不触发下载 |
| `map_type` | `administrative`、`terrain_hydrology` 或 `transport_samples` |
| `data_year` | 非空字符串，如 `"2020"` 或 `"1980–2020"`，不是 JSON 数字；每层另有自身年份，不能用此字段覆盖差异 |
| `language` | `zh` 或 `en`；标题、图例名和图注仍须按语言填写 |
| `layout` | `width_mm`、`height_mm`、`dpi`、`font_family`、`font_size_pt`；期刊要求优先 |
| `outputs` | 全新 `directory`、不含路径分隔符的 `basename`、所需 `formats`（png/svg/pdf） |
| `caption` | 必须为非空字符串，包含简明图注、数据署名、必要的年份/投影说明；程序按原文打印，不代替作者核对署名要求 |

## 图层

`layers` 为按 ID 引用的列表。每层需要 `id`、本地 `path`、`kind`、`role`、`coordinate_system`、`provenance`。

- `kind=vector`：GeoPackage、GeoJSON、Shapefile 或 ZIP。附带程序要求 GeoPackage 始终明确 `layer`，避免默认读取第一层；建议将使用的数据层单独保存到衍生文件。ZIP 应只包含一套配套 Shapefile，若有多个先确定唯一数据集。
- `kind=points_table`：CSV/Excel。显式给出 `x`、`y` 字段，`source_crs` 必填；Excel 可指定 `sheet_name`，默认首表。程序不交换字段、不猜度分秒、不地理编码、不计算缺失坐标。复杂表头应先生成规范化衍生表。
- `kind=dem`：单波段 GeoTIFF；额外给出 `elevation_unit`（`m` 或 `ft`）。数据说明还要记录垂直基准。第一版为高程色带渲染，不自动生成山体阴影或推导水系。
- `role` 取 `context`、`study_area`、`roads`、`water`、`samples`、`terrain`。道路/水系可为矢量；样点为点；研究区必须为面。
- `source_crs` 用来提供已知但文件中缺失的 CRS，或核对元数据；不得借它覆盖相冲突的已有 CRS。已有规范 GeoJSON 要结合其格式规范和提供者说明判定，不把所有扩展名为 GeoJSON 的文件都当成可信 WGS84。
- `coordinate_system` 取 `WGS84`、`CGCS2000`、`other`、`GCJ-02`、`BD-09`。必须由资料确认；附带程序拒绝后两种，需先采用有依据、已授权的转换流程并记录误差。
- `label_field` 可省略或为 `null`；表示显示标签的属性列。不能假设程序会自动避让全部标注。可选 `label` 指定图例显示名，省略时按角色和输出语言选择默认名。
- `style` 使用明确的 `facecolor`、`edgecolor`、`color`、`linewidth`、`markersize`、`alpha`；针对实际几何类型配置。填充 `none` 表示透明。不要把任意 Python 表达式写入配置。
- `provenance` 固定包含 `source`、`version`、`year`、`license`、`spatial_resolution`、`acquired_at`，全部使用非空字符串（例如年份用 `"2020"`）。精度、分辨率、比例尺是不同概念，写明单位和真实含义；不知道则为 `"unknown"`，不能编造。

附带程序对三类图的最低输入不同：行政图需研究边界；地形水系图还需至少一个 DEM 和一个水系图层；交通样点图还需道路与样点。必要图层必须列入对应地图框。缺少必需内容时调整图件目标或补数据，不通过更改角色名称伪装满足要求。

表格点图层片段（内容须按真实文件填写）：

```json
{
  "id": "samples",
  "path": "data/samples.csv",
  "kind": "points_table",
  "role": "samples",
  "source_crs": null,
  "coordinate_system": null,
  "x": "lon",
  "y": "lat",
  "label_field": "name",
  "style": {"color": "#b34436", "markersize": 14, "alpha": 1.0},
  "provenance": {
    "source": null, "version": "unknown", "year": null,
    "license": "unknown", "spatial_resolution": "unknown", "acquired_at": null
  }
}
```

## 地图框

每个 `panel` 给出唯一 `id`、`title`、`rect=[left,bottom,width,height]`（相对整页、0–1）、`crs`、`extent_wgs84=[west,south,east,north]`、按绘制顺序排列的 `layers`。

辅助程序只接受 2 或 3 个地图框；每框使用投影型 EPSG CRS（不能为 4326、4490 或 3857）。EPSG:4326/4490 可以是输入图层 CRS。地理包络框用于确定显示范围，投影变换可能改变最终可见范围；报告中应保留实际范围。

- `locator_for` 引用被定位的地图框 ID，例如定位小图引用主图。不得指向自己、形成环或引用不存在的地图框。
- `scale_bar_km` 为经过尺度评估的正数，`null` 表示该框不显示；最终应由作者确认是否满足图意和期刊要求。过长的比例尺应缩短，不能修改数字凑版面。
- `north_arrow` 为布尔值；真北方向应随地图框投影在箭头位置求得。

程序不自动处理跨日期变更线、多带复杂工程坐标、三维场景、全部字体缺字和完整标注避让。遇到这些情况使用定制路线，不扩大未经测试的支持声明。

## 执行与复现

只在授权后运行。运行前先核实代码版本、Python 环境和 JSON 内容。附带程序为严格选择本地坐标变换而要求 pyproj >= 3.5、PROJ >= 9.2；其他依赖及实际兼容性尚需导入和真实输入验证，不据此自动安装。模板还未填写、依赖未导入或数据不可用时，不将命令当作已完成测试。

运行结果应保留请求配置、输入文件哈希、软件版本、数据来源、实际地图框范围、检查警告和程序哈希。哈希证明文件身份，不证明数据正确。失败产生的目录应保留为失败记录，下次使用新目录，不能把残留图件当作完成结果。

正常运行输出 `config.used.json`、`run-report.json` 和所选图件格式。导出途中失败时保留 `.incomplete` 标记与可用的 `failure-report.json`，下一次改用新目录。后续目视验收结果另外记录，不把程序生成的 `needs_visual_review` 自动改成 `accepted`。
