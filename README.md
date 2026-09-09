# 智水监测

这是一个用于学习的智慧水利水情监测与预警系统。使用模拟水文数据，实现多水文站水情展示、预警、历史记录、趋势分析和地图可视化。

## 当前阶段：第十一阶段 — 预警中心 + 预警历史记录

在第十阶段降雨监测与综合预警基础上，增加预警记录存储、预警中心、预警历史、预警处理和重复预警抑制。

## 已完成功能

### 第六阶段：单站点实时监测仪表盘

1. 后端模拟水情数据 API
2. 前端实时水情数据卡片（水位、警戒水位、状态）
3. 水情预警面板（超警戒/正常状态切换）
4. ECharts 水位折线趋势图
5. 每 10 秒自动轮询刷新
6. 手动刷新按钮
7. Loading 状态与错误提示
8. 响应式布局

### 第七阶段：SQLite 历史数据持久化

1. SQLite 数据库自动创建与初始化
2. 水情数据自动写入数据库
3. 历史数据查询 API
4. 前端趋势图使用数据库历史数据
5. 首次启动自动生成 20 条初始模拟数据

### 第八阶段：多水文站管理

1. 5 个独立水文站（各有不同警戒水位和水位范围）
2. 水文站选择器（下拉菜单切换站点）
3. 水文站总览面板（显示所有站点实时状态）
4. 站点独立历史数据查询
5. 数据库自动迁移（保留历史数据）
6. 每个站点独立模拟数据生成

### 第九阶段：水文站地图/GIS可视化

1. Leaflet + React Leaflet 地图集成
2. OpenStreetMap 免费底图
3. 5 个水文站地图标记（Marker）
4. 地图缩放和拖动
5. 站点状态可视化（正常/警戒不同颜色）
6. 点击 Marker 显示 Popup 信息
7. 地图与水文站选择器双向联动
8. 自动刷新时更新地图 Marker 状态
9. 响应式地图（移动端自适应高度）

### 第十阶段：降雨监测与综合预警

1. 水情数据新增降雨量（rainfall）字段
2. 综合状态算法（水位 + 降雨量四级别判定）
3. 降雨等级函数（无明显降雨/小雨/中雨/大雨/暴雨）
4. 降雨卡片（当前降雨量显示）
5. ECharts 降雨量柱状图趋势
6. 综合预警面板（四级状态：正常/注意/警戒/超警）
7. 降雨统计模块（24h累计/最大单次/降雨等级）
8. 降雨统计 API（/api/rainfall-summary）
9. 地图 Marker 四级状态颜色
10. 水文站总览卡片显示降雨量
11. 数据库自动迁移（旧数据补零）

### 第十一阶段：预警中心 + 预警历史记录

1. warning_records 预警记录数据库表
2. 预警自动产生（注意/警戒/超警状态触发）
3. 重复预警抑制（5分钟内同站点同类型不重复）
4. 预警记录 API（/api/warnings）
5. 未处理预警 API（/api/warnings/active）
6. 预警统计 API（/api/warnings-summary）
7. 预警处理 API（POST /api/warnings/{id}/handle）
8. 前端预警中心（今日统计/最新预警/历史表格）
9. 预警处理按钮（标记为已处理）
10. 地图 Popup 显示最新预警
11. 10秒自动刷新同时更新预警中心

## 项目结构

```text
zhishui-monitor/
├── frontend/              # React 前端
│   ├── src/
│   │   ├── App.jsx        # 主页面组件（含地图）
│   │   ├── App.css        # 页面样式（含地图样式）
│   │   ├── index.css      # 全局样式
│   │   └── main.jsx       # 入口文件
│   ├── package.json
│   └── vite.config.js
├── backend/               # FastAPI 后端
│   ├── main.py            # API 路由
│   ├── database.py        # SQLite 数据库操作
│   ├── data/
│   │   └── water_monitor.db  # SQLite 数据库文件
│   └── requirements.txt
├── docs/                  # 学习笔记与阶段说明
├── README.md
└── .gitignore
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 前端 | React 19 + Vite 8 |
| 图表 | ECharts 6 + echarts-for-react |
| 地图 | Leaflet + React Leaflet |
| 底图 | OpenStreetMap（免费） |
| 后端 | FastAPI + uvicorn |
| 数据库 | SQLite（Python 内置 sqlite3） |
| 样式 | 纯手写 CSS |

## 水文站列表

| 站点编号 | 站点名称 | 警戒水位 | 模拟水位范围 | 经度 | 纬度 |
|----------|----------|----------|--------------|------|------|
| ST001 | 都江堰 | 5.0 m | 3.0 ~ 6.0 m | 103.64 | 30.99 |
| ST002 | 金堂 | 5.5 m | 3.5 ~ 6.5 m | 104.43 | 30.85 |
| ST003 | 温江 | 4.8 m | 2.5 ~ 5.5 m | 103.84 | 30.70 |
| ST004 | 龙泉驿 | 6.0 m | 4.0 ~ 7.0 m | 104.27 | 30.56 |
| ST005 | 新津 | 5.2 m | 3.2 ~ 6.2 m | 103.81 | 30.41 |

## 综合状态算法

状态由水位和降雨量共同判定：

| 级别 | 条件 |
|------|------|
| 超警 | 水位 ≥ 警戒水位 × 1.1 或 降雨 ≥ 50mm |
| 警戒 | 水位 ≥ 警戒水位 或 降雨 ≥ 30mm |
| 注意 | 水位 ≥ 警戒水位 × 0.8 或 降雨 ≥ 15mm |
| 正常 | 其他情况 |

## 预警规则

- 只有注意/警戒/超警状态产生预警记录，正常不产生
- 同一水文站5分钟内相同预警类型不重复创建
- 每次刷新时检查是否需要创建新预警

## 预警类型

| 状态 | 预警类型 | 说明 |
|------|----------|------|
| 注意 | 注意预警 | 水位或降雨达到注意阈值 |
| 警戒 | 警戒预警 | 水位或降雨达到警戒阈值 |
| 超警 | 超警预警 | 水位或降雨达到超警阈值 |

## API 接口

### GET /api/health

健康检查接口。

### GET /api/stations

获取所有水文站列表（含经纬度）。

### GET /api/water-data

获取指定水文站实时水情数据（含降雨量），同时写入数据库并创建预警。

**参数：**
- `station_id`（可选）：水文站编号，默认 ST001

### GET /api/water-data-all

获取所有水文站最新水情数据（含降雨量）。

### GET /api/water-history

获取指定水文站历史水情数据（含降雨量）。

**参数：**
- `station_id`（可选）：水文站编号，默认 ST001
- `limit`（可选）：返回数据条数，默认 20，最大 500

### GET /api/rainfall-summary

获取指定水文站降雨统计信息。

### GET /api/warnings

获取最近预警记录。

**参数：**
- `limit`（可选）：返回数据条数，默认 50，最大 500

**返回示例：**
```json
{
  "data": [
    {
      "id": 1,
      "station_id": "ST001",
      "station_name": "都江堰水文站",
      "water_level": 5.63,
      "warning_level": 5.0,
      "rainfall": 42.6,
      "warning_type": "超警预警",
      "warning_message": "都江堰水文站当前水位或降雨量达到超警阈值，请立即采取相应措施。",
      "created_at": "2026-09-09T21:05:00",
      "is_handled": 0
    }
  ]
}
```

### GET /api/warnings/active

获取当前未处理预警（is_handled = 0）。

### GET /api/warnings-summary

获取预警统计信息。

**返回示例：**
```json
{
  "today_total": 10,
  "attention": 4,
  "warning": 4,
  "critical": 2,
  "active": 6
}
```

### POST /api/warnings/{warning_id}/handle

将指定预警标记为已处理。

**返回：**
```json
{
  "success": true
}
```

## 数据库说明

数据库文件位于 `backend/data/water_monitor.db`，使用 SQLite。

### stations 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键，自增 |
| station_id | TEXT | 站点编号（唯一） |
| station_name | TEXT | 站点名称 |
| warning_level | REAL | 警戒水位（米） |
| latitude | REAL | 纬度 |
| longitude | REAL | 经度 |

### water_data 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键，自增 |
| station_id | TEXT | 站点编号 |
| station_name | TEXT | 站点名称 |
| water_level | REAL | 当前水位（米） |
| warning_level | REAL | 警戒水位（米） |
| rainfall | REAL | 降雨量（毫米） |
| status | TEXT | 综合状态（正常/注意/警戒/超警） |
| created_at | TEXT | 记录时间（ISO 8601） |

### warning_records 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | 主键，自增 |
| station_id | TEXT | 站点编号 |
| station_name | TEXT | 站点名称 |
| water_level | REAL | 当时水位（米） |
| warning_level | REAL | 警戒水位（米） |
| rainfall | REAL | 当时降雨量（毫米） |
| warning_type | TEXT | 预警类型（注意预警/警戒预警/超警预警） |
| warning_message | TEXT | 预警详细信息 |
| created_at | TEXT | 创建时间（ISO 8601） |
| is_handled | INTEGER | 是否已处理（0=未处理，1=已处理） |

## 地图功能说明

### Marker 状态颜色

- **绿色圆点**：水位正常
- **黄色圆点**：水位注意
- **橙色圆点**：水位警戒
- **红色圆点（脉冲动画）**：水位超警
- **蓝色大圆点**：当前选中的站点

### Popup 额外信息

- 如果站点存在未处理预警，Popup 会显示"最新预警：XX预警"

### 地图联动

1. **点击地图 Marker**：自动切换到该水文站
2. **下拉菜单切换**：地图自动飞移到对应站点位置
3. **水文站总览点击**：同步更新地图和数据

## 启动方法

### 1. 启动后端

```bash
cd backend

# 创建虚拟环境（首次）
python -m venv .venv

# 激活虚拟环境
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

后端服务运行在 http://127.0.0.1:8000

### 2. 启动前端

```bash
cd frontend

# 安装依赖（首次）
npm install

# 启动开发服务器
npm run dev
```

前端服务运行在 http://localhost:5173

### 3. 查看数据

```bash
# 获取水文站列表
curl http://127.0.0.1:8000/api/stations

# 获取指定站点实时数据（会创建预警）
curl http://127.0.0.1:8000/api/water-data?station_id=ST001

# 获取预警列表
curl http://127.0.0.1:8000/api/warnings?limit=50

# 获取未处理预警
curl http://127.0.0.1:8000/api/warnings/active

# 获取预警统计
curl http://127.0.0.1:8000/api/warnings-summary

# 处理预警
curl -X POST http://127.0.0.1:8000/api/warnings/1/handle
```

## 注意事项

1. 数据库文件（`.db`）和虚拟环境（`.venv`）已添加到 `.gitignore`
2. 地图使用 OpenStreetMap 免费底图，无需 API Key
3. 预警记录在每次刷新时自动创建（5分钟内同类型不重复）
4. 预警处理后不可撤销
5. 10秒自动刷新同时更新预警中心统计

## 公网部署

### 方案：GitHub + Render

```
GitHub 仓库
    ↓ 推送代码
Render 自动构建
    ↓ 部署后端
公网可访问网站
```

### 环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `CORS_ORIGINS` | CORS 允许的域名（逗号分隔） | `https://your-app.onrender.com` |
| `PORT` | Render 自动分配端口 | 不需要手动设置 |
| `VITE_API_BASE_URL` | 前端构建时的 API 地址 | `https://your-app.onrender.com` |

### Render 部署步骤

1. **推送到 GitHub**
2. **在 Render 创建 Web Service**
   - Build Command: `cd frontend && npm install && npm run build && cd ../backend && pip install -r requirements.txt`
   - Start Command: `cd backend && python -m uvicorn main:app --host 0.0.0.0 --port $PORT`
3. **设置环境变量**
   - `CORS_ORIGINS`: `https://your-app.onrender.com`
4. **部署完成**

### 数据库注意事项

- SQLite 数据库文件在 Render 免费版中**不会持久化**（每次重启会丢失）
- 首次启动时会自动创建数据库和初始数据
- 如需持久化，可升级 Render 付费版或使用外部数据库
