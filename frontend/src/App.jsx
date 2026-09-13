import { useCallback, useEffect, useState, useRef, useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import { MapContainer, TileLayer, Marker, Popup, useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import './App.css'

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

const STATUS_CLASSES = {
  '正常': 'status-normal',
  '注意': 'status-attention',
  '警戒': 'status-warning',
  '超警': 'status-danger',
}

const STATUS_COLORS = {
  '正常': '#16803c',
  '注意': '#d97706',
  '警戒': '#ea580c',
  '超警': '#dc2626',
}

function getStatusClass(status) {
  return STATUS_CLASSES[status] || 'status-normal'
}

function getStatusColor(status) {
  return STATUS_COLORS[status] || '#16803c'
}

// ── 第17阶段：智能预警中心辅助常量 ──
const ALERT_LEVEL_LABELS = {
  danger: '危险',
  warning: '预警',
  attention: '注意',
  normal: '正常',
}

const ALERT_STATUS_LABELS = {
  pending: '待处理',
  acknowledged: '已确认',
  resolved: '已解除',
}

const ALERT_HOURS_MAP = { '24h': 24, '7d': 168, '30d': 720 }

// ── 第15阶段：多站综合对比辅助函数 ──
const COMPARE_CHART_COLORS = ['#0b5d74', '#2563eb', '#d97706', '#16a34a', '#9333ea']

function riskLevelLabel(riskLevel) {
  const labels = {
    danger: '红色预警',
    warning: '橙色预警',
    attention: '黄色预警',
    normal: '正常',
  }
  return labels[riskLevel] || riskLevel || '—'
}

function trendLabel(trend) {
  const labels = { rising: '↑ 上升', falling: '↓ 下降', stable: '→ 平稳' }
  return labels[trend] || trend || '—'
}

function scrollToSection(id) {
  const el = document.getElementById(id)
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function formatAxisTime(value, is7d) {
  const d = new Date(value)
  const mm = (d.getMonth() + 1).toString().padStart(2, '0')
  const dd = d.getDate().toString().padStart(2, '0')
  const hh = d.getHours().toString().padStart(2, '0')
  const mi = d.getMinutes().toString().padStart(2, '0')
  return is7d ? `${mm}/${dd} ${hh}:${mi}` : `${hh}:${mi}`
}

function sourceText(source) {
  switch (source) {
    case 'chengdu_open_data':
      return '成都政务开放数据'
    case 'mock_fallback':
      return '模拟数据（自动降级）'
    case 'invalid_fallback':
      return '模拟数据'
    case 'mock':
      return '模拟数据'
    default:
      return source || '未知'
  }
}

function qualityText(quality) {
  switch ((quality || '').toLowerCase()) {
    case 'good':
    case 'valid':
    case 'normal':
      return '良好'
    case 'degraded':
    case 'fallback':
    case 'invalid':
      return '降级'
    default:
      return quality || '未知'
  }
}

function formatUpdateTime(value) {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('zh-CN', { hour12: false })
}

const AI_RISK_LABELS = { normal: '正常', attention: '注意', warning: '警戒', severe: '超警' }
const AI_RISK_CLASS = {
  normal: 'ai-risk-normal',
  attention: 'ai-risk-attention',
  warning: 'ai-risk-warning',
  severe: 'ai-risk-severe',
}

function aiRiskLabel(level) {
  return AI_RISK_LABELS[level] || level || '—'
}

function aiRiskClass(level) {
  return AI_RISK_CLASS[level] || 'ai-risk-normal'
}

function waterCompareOption(data, range) {
  const is7d = range === '168h'
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { left: 55, right: 20, top: 40, bottom: 50 },
    dataZoom: [{ type: 'inside', start: 0, end: 100 }],
    xAxis: {
      type: 'category',
      name: '时间',
      axisLabel: { rotate: is7d ? 35 : 0, formatter: (val) => formatAxisTime(val, is7d) },
      data: data.series.timestamps,
    },
    yAxis: { type: 'value', name: '水位（m）', axisLabel: { formatter: '{value} m' } },
    series: data.stations.map((s, index) => {
      const color = COMPARE_CHART_COLORS[index % COMPARE_CHART_COLORS.length]
      return {
        name: s.station_name,
        type: 'line',
        data: data.series.levels[s.station_id] || [],
        smooth: true,
        symbolSize: 5,
        lineStyle: { color, width: 2 },
        itemStyle: { color },
      }
    }),
  }
}

function rainfallCompareOption(data) {
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 0, data: ['累计降雨'] },
    grid: { left: 55, right: 20, top: 40, bottom: 50 },
    xAxis: { type: 'category', name: '站点', data: data.stations.map((s) => s.station_name) },
    yAxis: { type: 'value', name: '降雨量（mm）', axisLabel: { formatter: '{value} mm' } },
    series: [
      {
        name: '累计降雨',
        type: 'bar',
        barMaxWidth: 40,
        data: data.stations.map((s) => ({
          value: s.total_rainfall,
          itemStyle: {
            color: s.total_rainfall >= 50 ? '#dc2626'
              : s.total_rainfall >= 30 ? '#ea580c'
              : s.total_rainfall >= 15 ? '#d97706'
              : s.total_rainfall >= 5 ? '#2563eb'
              : '#94a3b8',
          },
        })),
      },
    ],
  }
}

function getMarkerIcon(status, isSelected) {
  if (isSelected) {
    return new L.DivIcon({
      className: 'custom-marker selected-marker',
      html: '<div class="marker-dot selected"></div>',
      iconSize: [30, 30],
      iconAnchor: [15, 15],
    })
  }
  const color = getStatusColor(status)
  return new L.DivIcon({
    className: 'custom-marker status-marker',
    html: `<div class="marker-dot" style="background-color:${color}"></div>`,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  })
}

function MapFlyTo({ center, zoom }) {
  const map = useMap()
  useEffect(() => {
    if (center) {
      map.flyTo(center, zoom || map.getZoom(), { duration: 1.0 })
    }
  }, [center, zoom, map])
  return null
}

function StationMap({ stations, overviewData, selectedStationId, onStationSelect, latestWarnings }) {
  const stationMap = useMemo(() => {
    const map = {}
    overviewData.forEach((item) => {
      map[item.station_id] = item
    })
    return map
  }, [overviewData])

  const warningMap = useMemo(() => {
    const map = {}
    if (latestWarnings) {
      latestWarnings.forEach((w) => {
        if (!map[w.station_id]) {
          map[w.station_id] = w
        }
      })
    }
    return map
  }, [latestWarnings])

  const selectedStation = useMemo(
    () => stations.find((s) => s.station_id === selectedStationId),
    [stations, selectedStationId]
  )

  const center = useMemo(() => {
    if (selectedStation && selectedStation.latitude && selectedStation.longitude) {
      return [selectedStation.latitude, selectedStation.longitude]
    }
    if (stations.length > 0 && stations[0].latitude) {
      return [stations[0].latitude, stations[0].longitude]
    }
    return [30.67, 104.06]
  }, [selectedStation, stations])

  const mapBounds = useMemo(() => {
    if (stations.length < 2) return null
    const lats = stations.filter((s) => s.latitude).map((s) => s.latitude)
    const lngs = stations.filter((s) => s.longitude).map((s) => s.longitude)
    if (lats.length === 0) return null
    return {
      center: [(Math.min(...lats) + Math.max(...lats)) / 2, (Math.min(...lngs) + Math.max(...lngs)) / 2],
      zoom: 9,
    }
  }, [stations])

  if (stations.length === 0) {
    return <div className="map-loading">水文站地图加载中...</div>
  }

  return (
    <section className="map-section" id="map" aria-label="水文站地图">
      <h2>水文站地图</h2>
      <div className="map-wrapper">
        <MapContainer
          center={mapBounds ? mapBounds.center : center}
          zoom={mapBounds ? mapBounds.zoom : 12}
          className="station-map"
          scrollWheelZoom={true}
        >
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />
          <MapFlyTo center={center} zoom={12} />
          {stations.map((station) => {
            if (!station.latitude || !station.longitude) return null
            const data = stationMap[station.station_id]
            const status = data ? data.status : '正常'
            const isSelected = station.station_id === selectedStationId
            const icon = getMarkerIcon(status, isSelected)
            const latestWarn = warningMap[station.station_id]

            return (
              <Marker
                key={station.station_id}
                position={[station.latitude, station.longitude]}
                icon={icon}
                eventHandlers={{
                  click: () => {
                    onStationSelect(station.station_id)
                  },
                }}
              >
                <Popup>
                  <div className="popup-content">
                    <h3>{station.station_name}</h3>
                    {data ? (
                      <>
                        <p>水位：{data.water_level.toFixed(2)} m</p>
                        <p>警戒：{data.warning_level.toFixed(2)} m</p>
                        <p>降雨：{(data.rainfall || 0).toFixed(1)} mm</p>
                        <p className={`popup-status ${getStatusClass(status)}`}>
                          状态：{status}
                        </p>
                        {latestWarn && (
                          <p className={`popup-warn ${getStatusClass(latestWarn.warning_type === '超警预警' ? '超警' : latestWarn.warning_type === '警戒预警' ? '警戒' : '注意')}`}>
                            最新预警：{latestWarn.warning_type}
                          </p>
                        )}
                      </>
                    ) : (
                      <p>暂无实时数据</p>
                    )}
                  </div>
                </Popup>
              </Marker>
            )
          })}
        </MapContainer>
      </div>
    </section>
  )
}

function LoginPage({ onLogin, onRegister, notice }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const response = await fetch(`${API_BASE}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      const data = await response.json()
      if (!response.ok) {
        setError(data.detail || '登录失败')
        return
      }
      onLogin(data.access_token, data.user)
    } catch {
      setError('无法连接服务器')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-container">
        <div className="login-header">
          <p className="login-system-label">智慧水利 · 水情监测</p>
          <h1>成都市智慧水利监测平台</h1>
          <p className="login-subtitle">请登录以访问监测系统</p>
        </div>
        {notice && <div className="login-notice">{notice}</div>}
        <form className="login-form" onSubmit={handleSubmit}>
          <div className="login-field">
            <label htmlFor="login-username">用户名</label>
            <input
              id="login-username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="请输入用户名"
              autoFocus
              required
            />
          </div>
          <div className="login-field">
            <label htmlFor="login-password">密码</label>
            <input
              id="login-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="请输入密码"
              required
            />
          </div>
          {error && <div className="login-error">{error}</div>}
          <button type="submit" className="login-btn" disabled={loading}>
            {loading ? '登录中...' : '登 录'}
          </button>
        </form>
        <p className="login-register-tip">
          没有账号？
          <button type="button" className="link-btn" onClick={onRegister}>立即注册</button>
        </p>
        <p className="login-help">学校师生均可注册普通账号，注册后即可查看成都地区水文监测数据、预警与报表。</p>
      </div>
    </div>
  )
}

function RegisterPage({ onBack, onRegistered }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')

    if (!username.trim()) {
      setError('用户名不能为空')
      return
    }
    if (username.trim().length < 3 || username.trim().length > 30) {
      setError('用户名长度需在3到30个字符之间')
      return
    }
    if (password.length < 8) {
      setError('密码长度不能少于8位')
      return
    }
    if (password !== confirmPassword) {
      setError('两次密码输入不一致')
      return
    }

    setLoading(true)
    try {
      const response = await fetch(`${API_BASE}/api/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), password, confirmPassword }),
      })
      const data = await response.json()
      if (!response.ok) {
        setError(data.detail || '注册失败')
        return
      }
      onRegistered()
    } catch {
      setError('无法连接服务器')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-container">
        <div className="login-header">
          <p className="login-system-label">智慧水利 · 水情监测</p>
          <h1>注册账号</h1>
          <p className="login-subtitle">创建一个普通用户账号</p>
        </div>
        <form className="login-form" onSubmit={handleSubmit}>
          <div className="login-field">
            <label htmlFor="reg-username">用户名</label>
            <input
              id="reg-username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="3~30位用户名"
              autoFocus
              required
            />
          </div>
          <div className="login-field">
            <label htmlFor="reg-password">密码</label>
            <input
              id="reg-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少8位密码"
              required
            />
          </div>
          <div className="login-field">
            <label htmlFor="reg-confirm">确认密码</label>
            <input
              id="reg-confirm"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder="请再次输入密码"
              required
            />
          </div>
          {error && <div className="login-error">{error}</div>}
          <button type="submit" className="login-btn" disabled={loading}>
            {loading ? '注册中...' : '注 册'}
          </button>
        </form>
        <p className="login-register-tip">
          已有账号？
          <button type="button" className="link-btn" onClick={onBack}>返回登录</button>
        </p>
      </div>
    </div>
  )
}

function AdminPanel({ token, onBack, onLogout, onGoStation }) {
  const [tab, setTab] = useState('users')
  const [users, setUsers] = useState([])
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState('user')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const [alerts, setAlerts] = useState([])
  const [alertsLoading, setAlertsLoading] = useState(false)

  const [sourceInfo, setSourceInfo] = useState(null)
  const [qualityStats, setQualityStats] = useState(null)
  const [stationQuality, setStationQuality] = useState([])
  const [sourceLoading, setSourceLoading] = useState(false)

  const handleUnauthorized = useCallback((response) => {
    if (response && response.status === 401) {
      if (onLogout) onLogout()
      return true
    }
    return false
  }, [onLogout])

  const loadUsers = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/users`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (handleUnauthorized(response)) return
      if (response.ok) {
        const result = await response.json()
        setUsers(result.data)
      }
    } catch {
      // ignore
    }
  }, [token, handleUnauthorized])

  useEffect(() => {
    loadUsers()
  }, [loadUsers])

  const loadAlertsForAdmin = useCallback(async () => {
    setAlertsLoading(true)
    try {
      const [pendingRes, ackRes] = await Promise.allSettled([
        fetch(`${API_BASE}/api/alerts?hours=720&status=pending&limit=100`).then((r) => r.json()),
        fetch(`${API_BASE}/api/alerts?hours=720&status=acknowledged&limit=100`).then((r) => r.json()),
      ])
      const merged = []
      if (pendingRes.status === 'fulfilled' && Array.isArray(pendingRes.value.items)) merged.push(...pendingRes.value.items)
      if (ackRes.status === 'fulfilled' && Array.isArray(ackRes.value.items)) merged.push(...ackRes.value.items)
      merged.sort((a, b) => new Date(b.created_at) - new Date(a.created_at))
      setAlerts(merged)
    } catch {
      setAlerts([])
    } finally {
      setAlertsLoading(false)
    }
  }, [])

  const handleAdminAck = useCallback(async (alertId) => {
    try {
      const response = await fetch(`${API_BASE}/api/alerts/${alertId}/acknowledge`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (handleUnauthorized(response)) return
      if (response.ok) await loadAlertsForAdmin()
    } catch {
      // ignore
    }
  }, [token, loadAlertsForAdmin, handleUnauthorized])

  const handleAdminResolve = useCallback(async (alertId) => {
    try {
      const response = await fetch(`${API_BASE}/api/alerts/${alertId}/resolve`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (handleUnauthorized(response)) return
      if (response.ok) await loadAlertsForAdmin()
    } catch {
      // ignore
    }
  }, [token, loadAlertsForAdmin, handleUnauthorized])

  const loadSourceInfo = useCallback(async () => {
    setSourceLoading(true)
    try {
      const [dsRes, dqRes, allRes] = await Promise.allSettled([
        fetch(`${API_BASE}/api/data-source`).then((r) => r.json()),
        fetch(`${API_BASE}/api/data-quality`).then((r) => r.json()),
        fetch(`${API_BASE}/api/water-data-all`).then((r) => r.json()),
      ])
      setSourceInfo(dsRes.status === 'fulfilled' ? dsRes.value : null)
      const dq = dqRes.status === 'fulfilled' ? dqRes.value : null
      setQualityStats(dq ? dq.data_quality || dq.quality : null)
      const all = allRes.status === 'fulfilled' && Array.isArray(allRes.value.data) ? allRes.value.data : []
      setStationQuality(all)
    } catch {
      setSourceInfo(null)
      setQualityStats(null)
      setStationQuality([])
    } finally {
      setSourceLoading(false)
    }
  }, [])

  const handleCreate = async (e) => {
    e.preventDefault()
    setError('')
    setSuccess('')
    try {
      const response = await fetch(`${API_BASE}/api/users`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ username: newUsername, password: newPassword, role: newRole }),
      })
      if (handleUnauthorized(response)) return
      const data = await response.json()
      if (!response.ok) {
        setError(data.detail || '创建失败')
        return
      }
      setSuccess('用户创建成功')
      setNewUsername('')
      setNewPassword('')
      setNewRole('user')
      await loadUsers()
    } catch {
      setError('创建失败')
    }
  }

  const handleDelete = async (userId) => {
    if (!confirm('确定要删除该用户吗？')) return
    try {
      const response = await fetch(`${API_BASE}/api/users/${userId}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (handleUnauthorized(response)) return
      if (response.ok) {
        await loadUsers()
      }
    } catch {
      // ignore
    }
  }

  const handleRoleChange = async (userId, role) => {
    try {
      const response = await fetch(`${API_BASE}/api/users/${userId}/role`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ role }),
      })
      if (handleUnauthorized(response)) return
      if (response.ok) {
        await loadUsers()
      }
    } catch {
      // ignore
    }
  }

  return (
    <div className="user-manager-page">
      <div className="user-manager-container admin-panel">
        <div className="user-manager-header">
          <h1>管理后台</h1>
          <button className="back-btn" onClick={onBack}>返回监测大屏</button>
        </div>

        <div className="admin-tabs" role="tablist" aria-label="管理功能">
          <button
            type="button"
            className={`admin-tab-btn ${tab === 'users' ? 'active' : ''}`}
            onClick={() => setTab('users')}
          >用户管理</button>
          <button
            type="button"
            className={`admin-tab-btn ${tab === 'alerts' ? 'active' : ''}`}
            onClick={() => {
              setTab('alerts')
              loadAlertsForAdmin()
            }}
          >告警处理</button>
          <button
            type="button"
            className={`admin-tab-btn ${tab === 'source' ? 'active' : ''}`}
            onClick={() => {
              setTab('source')
              loadSourceInfo()
            }}
          >数据源与质量</button>
        </div>

        {tab === 'users' && (
          <>
        <form className="create-user-form" onSubmit={handleCreate}>
          <h2>创建新用户</h2>
          <div className="form-row">
            <div className="form-field">
              <label>用户名</label>
              <input
                type="text"
                value={newUsername}
                onChange={(e) => setNewUsername(e.target.value)}
                placeholder="用户名"
                required
              />
            </div>
            <div className="form-field">
              <label>密码</label>
              <input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                placeholder="密码"
                required
              />
            </div>
            <div className="form-field">
              <label>角色</label>
              <select value={newRole} onChange={(e) => setNewRole(e.target.value)}>
                <option value="user">普通用户</option>
                <option value="admin">管理员</option>
              </select>
            </div>
            <button type="submit" className="create-btn">创建</button>
          </div>
          {error && <div className="form-error">{error}</div>}
          {success && <div className="form-success">{success}</div>}
        </form>

        <div className="user-list-section">
          <h2>用户列表</h2>
          <div className="table-scroll">
          <table className="user-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>用户名</th>
                <th>角色</th>
                <th>状态</th>
                <th>创建时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td>{u.id}</td>
                  <td>{u.username}</td>
                  <td>
                    <select
                      value={u.role}
                      onChange={(e) => handleRoleChange(u.id, e.target.value)}
                      className="role-select"
                    >
                      <option value="user">普通用户</option>
                      <option value="admin">管理员</option>
                    </select>
                  </td>
                  <td>{u.is_active ? '启用' : '禁用'}</td>
                  <td>{new Date(u.created_at).toLocaleString('zh-CN')}</td>
                  <td>
                    {u.username !== 'admin' && (
                      <button className="delete-btn" onClick={() => handleDelete(u.id)}>
                        删除
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
          </>
        )}

        {tab === 'alerts' && (
          <div className="admin-alerts-tab">
            <h2>未处理 / 已确认预警（近 30 天）</h2>
            {alertsLoading && (
              <p className="notice loading">正在加载预警...</p>
            )}
            {!alertsLoading && alerts.length === 0 && (
              <p className="notice insufficient">当前没有待处理的预警</p>
            )}
            {!alertsLoading &&
              alerts.map((a) => (
                <article
                  key={a.id}
                  className={`alert-card alert-level-${a.warning_level}`}
                >
                  <div className="alert-card-top">
                    <div className="alert-card-left">
                      <span className={`alert-level alert-level-${a.warning_level}`}>
                        {ALERT_LEVEL_LABELS[a.warning_level] || a.warning_level}
                      </span>
                      <span className="alert-title">
                        {a.title || `${a.station_name}水情预警`}
                      </span>
                      <span className={`alert-status alert-status-${a.status}`}>
                        {ALERT_STATUS_LABELS[a.status] || a.status}
                      </span>
                    </div>
                  </div>
                  <div className="alert-card-meta">
                    <span>站点：{a.station_name}　时间：{new Date(a.created_at).toLocaleString('zh-CN')}</span>
                    <button
                      type="button"
                      className="alert-station-link"
                      onClick={() => onGoStation && onGoStation(a.station_id)}
                    >
                      查看站点
                    </button>
                  </div>
                  <p className="alert-message">{a.message}</p>
                  <div className="alert-metrics">
                    <span>当前水位 <strong>{Number(a.water_level).toFixed(2)} m</strong></span>
                    <span>警戒水位 <strong>{Number(a.warning_level_value).toFixed(2)} m</strong></span>
                    <span>降雨量 <strong>{Number(a.rainfall).toFixed(1)} mm</strong></span>
                  </div>
                  <div className="alert-actions">
                    {a.status !== 'acknowledged' && (
                      <button
                        type="button"
                        className="alert-btn alert-btn-acknowledge"
                        onClick={() => handleAdminAck(a.id)}
                      >
                        确认预警
                      </button>
                    )}
                    <button
                      type="button"
                      className="alert-btn alert-btn-resolve"
                      onClick={() => handleAdminResolve(a.id)}
                    >
                      解除预警
                    </button>
                  </div>
                </article>
              ))}
          </div>
        )}

        {tab === 'source' && (
          <div className="admin-source-tab">
            <h2>当前数据源状态</h2>
            {sourceLoading && (
              <p className="notice loading">正在加载数据源状态...</p>
            )}
            {!sourceLoading && (
              <>
                {sourceInfo ? (
                  <div className="source-status-card">
                    <div className="source-status-head">
                      <span className="source-group-label">数据来源</span>
                      <strong>{sourceInfo.status ? sourceInfo.status.display_name : sourceInfo.display_name || sourceInfo.source || '—'}</strong>
                      <span className={`config-badge ${sourceInfo.configured ? 'config-yes' : 'config-no'}`}>
                        {sourceInfo.configured ? '已启用' : '未启用'}
                      </span>
                    </div>
                    <div className="source-status-grid">
                      <div>
                        <span>数据质量</span>
                        <strong>{sourceInfo.data_quality == null ? '—' : qualityText(sourceInfo.data_quality)}</strong>
                      </div>
                      <div>
                        <span>可达性</span>
                        <strong>{typeof sourceInfo.reachable === 'boolean' ? (sourceInfo.reachable ? '可达' : '不可达') : '—'}</strong>
                      </div>
                      <div>
                        <span>来源标识</span>
                        <strong>{sourceInfo.source || '—'}</strong>
                      </div>
                    </div>
                    {sourceInfo.status && sourceInfo.status.reason && (
                      <p className="source-reason">{sourceInfo.status.reason}</p>
                    )}
                  </div>
                ) : (
                  <p className="notice error">数据源状态获取失败</p>
                )}

                <h2>数据质量统计</h2>
                {qualityStats ? (
                  <div className="quality-stats-grid">
                    <div className="stat-card">
                      <span className="stat-label">总记录</span>
                      <span className="stat-value">{qualityStats.total}</span>
                    </div>
                    <div className="stat-card">
                      <span className="stat-label">良好(valid)</span>
                      <span className="stat-value">{qualityStats.valid}</span>
                    </div>
                    <div className="stat-card">
                      <span className="stat-label">良好(good)</span>
                      <span className="stat-value">{qualityStats.good}</span>
                    </div>
                    <div className="stat-card">
                      <span className="stat-label">降级(degraded)</span>
                      <span className="stat-value">{qualityStats.degraded}</span>
                    </div>
                    <div className="stat-card">
                      <span className="stat-label">无效(invalid)</span>
                      <span className="stat-value">{qualityStats.invalid}</span>
                    </div>
                    <div className="stat-card">
                      <span className="stat-label">回退(fallback)</span>
                      <span className="stat-value">{qualityStats.fallback}</span>
                    </div>
                  </div>
                ) : (
                  <p className="notice error">数据质量统计获取失败</p>
                )}

                <h2>各站点最新数据来源</h2>
                {stationQuality.length > 0 ? (
                  <div className="table-scroll">
                    <table className="user-table quality-table">
                      <thead>
                        <tr>
                          <th>站点</th>
                          <th>数据来源</th>
                          <th>数据质量</th>
                          <th>更新时间</th>
                        </tr>
                      </thead>
                      <tbody>
                        {stationQuality.map((s) => (
                          <tr key={s.station_id}>
                            <td>{s.station_name}</td>
                            <td>{sourceText(s.source)}</td>
                            <td>{qualityText(s.data_quality)}</td>
                            <td>{formatUpdateTime(s.updated_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="notice insufficient">暂无站点数据</p>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function MonitorApp({ user, token, onLogout }) {
  const [stations, setStations] = useState([])
  const [selectedStationId, setSelectedStationId] = useState('ST001')
  const [waterData, setWaterData] = useState(null)
  const [trendData, setTrendData] = useState([])
  const [overviewData, setOverviewData] = useState([])
  const [rainfallSummary, setRainfallSummary] = useState(null)
  const [warnings, setWarnings] = useState([])
  const [warningSummary, setWarningSummary] = useState(null)
  const [latestWarnings, setLatestWarnings] = useState([])
  const [isLoading, setIsLoading] = useState(true)
  const [errorMessage, setErrorMessage] = useState('')
  const [dataErrorMessage, setDataErrorMessage] = useState('')
  const [aiAnalysis, setAiAnalysis] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState('')
  const [showAdminPanel, setShowAdminPanel] = useState(false)
  const timerRef = useRef(null)

  // ── 第14阶段：历史分析状态 ──
  const [historyRange, setHistoryRange] = useState('24h')
  const [analysisData, setAnalysisData] = useState(null)
  const [analysisLoading, setAnalysisLoading] = useState(false)

  // ── 第15阶段：多站综合对比状态 ──
  const [comparisonRange, setComparisonRange] = useState('24h')
  const [comparisonData, setComparisonData] = useState(null)
  const [comparisonLoading, setComparisonLoading] = useState(false)

  // ── 第16阶段：数据报表状态 ──
  const [reportRange, setReportRange] = useState('24h')
  const [reportStation, setReportStation] = useState('')
  const [reportData, setReportData] = useState(null)
  const [reportStats, setReportStats] = useState(null)
  const [reportLoading, setReportLoading] = useState(false)

  // ── 第17阶段：智能预警中心状态 ──
  const [alertRange, setAlertRange] = useState('24h')
  const [alertStation, setAlertStation] = useState('')
  const [alertLevel, setAlertLevel] = useState('')
  const [alertStatus, setAlertStatus] = useState('')
  const [alertItems, setAlertItems] = useState([])
  const [alertSummary, setAlertSummary] = useState(null)
  const [alertLoading, setAlertLoading] = useState(false)
  const [alertExpandedId, setAlertExpandedId] = useState(null)

  const loadStations = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/stations`)
      if (response.ok) {
        const result = await response.json()
        if (Array.isArray(result.data)) {
          setStations(result.data)
        }
      }
    } catch {
      // ignore
    }
  }, [])

  const loadOverview = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/water-data-all`)
      if (response.ok) {
        const result = await response.json()
        if (Array.isArray(result.data)) {
          setOverviewData(result.data)
        }
      }
    } catch {
      // ignore
    }
  }, [])

  const loadRainfallSummary = useCallback(async (stationId) => {
    try {
      const response = await fetch(`${API_BASE}/api/rainfall-summary?station_id=${stationId}`)
      if (response.ok) {
        const result = await response.json()
        setRainfallSummary(result)
      }
    } catch {
      // ignore
    }
  }, [])

  const loadWarnings = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/warnings?limit=50`)
      if (response.ok) {
        const result = await response.json()
        if (Array.isArray(result.data)) {
          setWarnings(result.data)
        }
      }
    } catch {
      // ignore
    }
  }, [])

  const loadWarningSummary = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/warnings-summary`)
      if (response.ok) {
        const result = await response.json()
        setWarningSummary(result)
      }
    } catch {
      // ignore
    }
  }, [])

  const loadLatestWarnings = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/warnings/active`)
      if (response.ok) {
        const result = await response.json()
        if (Array.isArray(result.data)) {
          setLatestWarnings(result.data)
        }
      }
    } catch {
      // ignore
    }
  }, [])

  const handleWarningClick = useCallback(async (warningId) => {
    try {
      const response = await fetch(`${API_BASE}/api/warnings/${warningId}/handle`, {
        method: 'POST',
      })
      if (response.ok) {
        await loadWarnings()
        await loadWarningSummary()
        await loadLatestWarnings()
      }
    } catch {
      // ignore
    }
  }, [loadWarnings, loadWarningSummary, loadLatestWarnings])

  const loadHistoryData = useCallback(async (stationId) => {
    try {
      const response = await fetch(`${API_BASE}/api/water-history?station_id=${stationId}&limit=50`)
      if (!response.ok) {
        throw new Error('获取历史数据失败')
      }
      const result = await response.json()
      if (Array.isArray(result.data)) {
        setTrendData(
          result.data.map((item) => ({
            time: new Date(item.created_at).toLocaleTimeString('zh-CN', {
              hour: '2-digit',
              minute: '2-digit',
              second: '2-digit',
            }),
            waterLevel: item.water_level,
            warningLevel: item.warning_level,
            rainfall: item.rainfall || 0,
          }))
        )
      }
    } catch {
      // 历史数据加载失败时保持现有趋势数据
    }
  }, [])

  const loadAlertSummary = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/alerts/summary`)
      if (response.ok) {
        const result = await response.json()
        setAlertSummary(result)
      }
    } catch {
      // ignore
    }
  }, [])

  const loadWaterData = useCallback(async (stationId) => {
    setIsLoading(true)
    setErrorMessage('')
    setDataErrorMessage('')

    try {
      const response = await fetch(`${API_BASE}/api/water-data?station_id=${stationId}`)

      if (!response.ok) {
        throw new Error('服务器返回了错误响应')
      }

      const data = await response.json()

      if (data.error) {
        setErrorMessage(data.error)
        return
      }

      const hasValidLevels =
        typeof data.water_level === 'number' &&
        Number.isFinite(data.water_level) &&
        typeof data.warning_level === 'number' &&
        Number.isFinite(data.warning_level)

      if (!hasValidLevels) {
        setDataErrorMessage('水情数据异常')
        return
      }

      setWaterData(data)
      await Promise.allSettled([
        loadHistoryData(stationId),
        loadOverview(),
        loadRainfallSummary(stationId),
        loadWarnings(),
        loadWarningSummary(),
        loadLatestWarnings(),
        loadAlertSummary(),
      ])
    } catch {
      setErrorMessage('无法连接水情监测服务器')
    } finally {
      setIsLoading(false)
    }
  }, [loadHistoryData, loadOverview, loadRainfallSummary, loadWarnings, loadWarningSummary, loadLatestWarnings, loadAlertSummary])

  const loadHistoryAnalysis = useCallback(async (stationId, hours) => {
    setAnalysisLoading(true)
    setAnalysisData(null)
    try {
      const [historyRes, trendRes, statsRes, forecastRes] = await Promise.allSettled([
        fetch(`${API_BASE}/api/history?station_id=${stationId}&hours=${hours}`).then(r => r.json()),
        fetch(`${API_BASE}/api/trend?station_id=${stationId}&hours=${hours}`).then(r => r.json()),
        fetch(`${API_BASE}/api/statistics?station_id=${stationId}&hours=${hours}`).then(r => r.json()),
        fetch(`${API_BASE}/api/forecast?station_id=${stationId}&hours=${hours}`).then(r => r.json()),
      ])
      setAnalysisData({
        history: historyRes.status === 'fulfilled' ? historyRes.value : null,
        trend: trendRes.status === 'fulfilled' ? trendRes.value : null,
        statistics: statsRes.status === 'fulfilled' ? statsRes.value : null,
        forecast: forecastRes.status === 'fulfilled' ? forecastRes.value : null,
      })
    } catch {
      setAnalysisData(null)
    } finally {
      setAnalysisLoading(false)
    }
  }, [])

  const loadComparison = useCallback(async (hours) => {
    setComparisonLoading(true)
    setComparisonData(null)
    try {
      const response = await fetch(`${API_BASE}/api/comparison?hours=${hours}`)
      if (response.ok) {
        const data = await response.json()
        setComparisonData(data)
      }
    } catch {
      setComparisonData(null)
    } finally {
      setComparisonLoading(false)
    }
  }, [])

  const loadAiAnalysis = useCallback(async () => {
    setAiLoading(true)
    setAiError('')
    try {
      const response = await fetch(`${API_BASE}/api/ai-analysis`)
      if (!response.ok) throw new Error('AI 分析接口返回错误')
      const data = await response.json()
      if (!data || typeof data.risk_level !== 'string') throw new Error('AI 分析响应异常')
      setAiAnalysis(data)
    } catch {
      setAiAnalysis(null)
      setAiError('AI 分析暂不可用')
    } finally {
      setAiLoading(false)
    }
  }, [])

  const loadReport = useCallback(async (stationId, hours) => {
    setReportLoading(true)
    setReportData(null)
    setReportStats(null)
    try {
      const params = new URLSearchParams({ hours: String(hours) })
      if (stationId) params.set('station_id', stationId)
      const [dataRes, statsRes] = await Promise.allSettled([
        fetch(`${API_BASE}/api/report?${params}`).then(r => r.json()),
        fetch(`${API_BASE}/api/report/statistics?${params}`).then(r => r.json()),
      ])
      setReportData(dataRes.status === 'fulfilled' ? dataRes.value : null)
      setReportStats(statsRes.status === 'fulfilled' ? statsRes.value : null)
    } catch {
      setReportData(null)
      setReportStats(null)
    } finally {
      setReportLoading(false)
    }
  }, [])

  const loadAlerts = useCallback(async () => {
    setAlertLoading(true)
    try {
      const params = new URLSearchParams({ hours: String(ALERT_HOURS_MAP[alertRange] || 24) })
      if (alertStation) params.set('station_id', alertStation)
      if (alertLevel) params.set('level', alertLevel)
      if (alertStatus) params.set('status', alertStatus)
      const response = await fetch(`${API_BASE}/api/alerts?${params}`)
      if (response.ok) {
        const result = await response.json()
        setAlertItems(Array.isArray(result.items) ? result.items : [])
      } else {
        setAlertItems([])
      }
    } catch {
      setAlertItems([])
    } finally {
      setAlertLoading(false)
    }
  }, [alertStation, alertLevel, alertStatus, alertRange])

  const handleAlertAcknowledge = useCallback(async (alertId) => {
    try {
      const response = await fetch(`${API_BASE}/api/alerts/${alertId}/acknowledge`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (response.status === 401) {
        onLogout()
        return
      }
      if (response.ok) {
        await loadAlerts()
        await loadAlertSummary()
      }
    } catch {
      // ignore
    }
  }, [token, loadAlerts, loadAlertSummary, onLogout])

  const handleAlertResolve = useCallback(async (alertId) => {
    try {
      const response = await fetch(`${API_BASE}/api/alerts/${alertId}/resolve`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (response.status === 401) {
        onLogout()
        return
      }
      if (response.ok) {
        await loadAlerts()
        await loadAlertSummary()
      }
    } catch {
      // ignore
    }
  }, [token, loadAlerts, loadAlertSummary, onLogout])

  useEffect(() => {
    loadStations()
  }, [loadStations])

  useEffect(() => {
    loadWaterData(selectedStationId)

    if (timerRef.current) {
      clearInterval(timerRef.current)
    }
    timerRef.current = setInterval(() => {
      loadWaterData(selectedStationId)
    }, 10000)

    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current)
      }
    }
  }, [selectedStationId, loadWaterData])

  useEffect(() => {
    const hours = historyRange === '24h' ? 24 : 168
    loadHistoryAnalysis(selectedStationId, hours)
  }, [selectedStationId, historyRange, loadHistoryAnalysis])

  useEffect(() => {
    const hours = comparisonRange === '24h' ? 24 : 168
    loadComparison(hours)
  }, [comparisonRange, loadComparison])

  useEffect(() => {
    const hoursMap = { '24h': 24, '7d': 168, '30d': 720 }
    const hours = hoursMap[reportRange] || 24
    loadReport(reportStation, hours)
  }, [reportRange, reportStation, loadReport])

  useEffect(() => {
    loadAlerts()
  }, [loadAlerts])

  const handleStationChange = (e) => {
    setSelectedStationId(e.target.value)
  }

  const downloadReport = (format) => {
    const hoursMap = { '24h': 24, '7d': 168, '30d': 720 }
    const hours = hoursMap[reportRange] || 24
    const params = new URLSearchParams({ hours: String(hours) })
    if (reportStation) params.set('station_id', reportStation)
    window.open(`${API_BASE}/api/report/export/${format}?${params}`, '_blank')
  }

  const status = waterData ? waterData.status : '正常'
  const levelDifference = waterData
    ? Math.abs(waterData.water_level - waterData.warning_level)
    : 0

  const systemSource = (waterData && waterData.source) || (overviewData[0] && overviewData[0].source) || 'mock'
  const systemQuality = (waterData && waterData.data_quality) || (overviewData[0] && overviewData[0].data_quality) || 'valid'
  const isMockSource = ['mock', 'mock_fallback', 'invalid_fallback'].includes(systemSource)

  const statusCounts = useMemo(() => {
    const counts = {}
    overviewData.forEach((item) => {
      const s = item.status || '正常'
      counts[s] = (counts[s] || 0) + 1
    })
    return counts
  }, [overviewData])

  const activeWarnTotal = warningSummary && warningSummary.active != null ? warningSummary.active : latestWarnings.length

  const waterChartOption = {
    tooltip: { trigger: 'axis' },
    grid: { left: 55, right: 30, top: 45, bottom: 45 },
    xAxis: {
      type: 'category',
      name: '时间',
      data: trendData.map((point) => point.time),
    },
    yAxis: {
      type: 'value',
      name: '水位（m）',
      axisLabel: { formatter: '{value} m' },
    },
    series: [
      {
        name: '水位',
        type: 'line',
        data: trendData.map((point, index) => {
          const isLatestOver = index === trendData.length - 1 && point.waterLevel >= point.warningLevel
          return isLatestOver
            ? { value: point.waterLevel, symbolSize: 13, itemStyle: { color: '#dc2626' } }
            : point.waterLevel
        }),
        smooth: true,
        symbolSize: 8,
        lineStyle: { color: '#0b5d74', width: 3 },
        itemStyle: { color: '#0b5d74' },
        markLine: {
          symbol: 'none',
          lineStyle: { color: '#d97706', type: 'dashed' },
          label: { formatter: '警戒水位' },
          data: trendData.length
            ? [{ yAxis: trendData[trendData.length - 1].warningLevel }]
            : [],
        },
      },
    ],
  }

  const rainfallChartOption = {
    tooltip: { trigger: 'axis' },
    grid: { left: 55, right: 30, top: 45, bottom: 45 },
    xAxis: {
      type: 'category',
      name: '时间',
      data: trendData.map((point) => point.time),
    },
    yAxis: {
      type: 'value',
      name: '降雨量（mm）',
      axisLabel: { formatter: '{value} mm' },
    },
    series: [
      {
        name: '降雨量',
        type: 'bar',
        data: trendData.map((point) => ({
          value: point.rainfall,
          itemStyle: {
            color: point.rainfall >= 50 ? '#dc2626'
              : point.rainfall >= 30 ? '#ea580c'
              : point.rainfall >= 15 ? '#d97706'
              : point.rainfall >= 5 ? '#2563eb'
              : '#94a3b8',
          },
        })),
        barMaxWidth: 20,
      },
    ],
  }

  if (showAdminPanel) {
    return (
      <AdminPanel
        token={token}
        onBack={() => setShowAdminPanel(false)}
        onLogout={onLogout}
        onGoStation={(stationId) => {
          setSelectedStationId(stationId)
          setShowAdminPanel(false)
        }}
      />
    )
  }

  return (
    <main className="monitor-page">
      <section className="monitor-panel">
        <header className="page-header">
          <div className="header-top">
            <div>
              <p className="system-label">智慧水利 · 水情监测</p>
              <h1>成都市智慧水利监测平台</h1>
              <p className="subtitle">成都地区雨情水情综合监测与预警系统</p>
            </div>
            <div className="user-info">
              <span className="user-role-badge">{user.role === 'admin' ? '管理员' : '用户'}</span>
              <span className="user-name">{user.username}</span>
              <button className="logout-btn" onClick={onLogout}>退出</button>
            </div>
          </div>
        </header>

        <nav className="top-nav" aria-label="页面导航">
          <button type="button" onClick={() => scrollToSection('overview')}>总览</button>
          <button type="button" onClick={() => scrollToSection('ai')}>AI 分析</button>
          <button type="button" onClick={() => scrollToSection('map')}>地图</button>
          <button type="button" onClick={() => scrollToSection('current')}>实时数据</button>
          <button type="button" onClick={() => scrollToSection('rainfall')}>雨情</button>
          <button type="button" onClick={() => scrollToSection('alerts')}>预警</button>
          <button type="button" onClick={() => scrollToSection('history')}>历史分析</button>
          <button type="button" onClick={() => scrollToSection('compare')}>站点比较</button>
          <button type="button" onClick={() => scrollToSection('report')}>数据报表</button>
          {user.role === 'admin' && (
            <button type="button" className="top-nav-admin" onClick={() => setShowAdminPanel(true)}>管理后台</button>
          )}
        </nav>

        <div className={`demo-banner ${isMockSource ? 'banner-mock' : 'banner-real'}`} aria-label="数据来源说明">
          <span className="demo-banner-label">数据来源：{sourceText(systemSource)}</span>
          <span className="demo-banner-quality">数据质量：{qualityText(systemQuality)}</span>
          {isMockSource && <span className="demo-banner-note">当前为模拟演示数据（非真实官方观测结果），仅供教学与演示使用</span>}
        </div>

        {stations.length > 0 && (
          <div className="station-selector">
            <label htmlFor="station-select">当前水文站：</label>
            <select
              id="station-select"
              value={selectedStationId}
              onChange={handleStationChange}
            >
              {stations.map((s) => (
                <option key={s.station_id} value={s.station_id}>
                  {s.station_name}
                </option>
              ))}
            </select>
          </div>
        )}

        {overviewData.length > 0 && (
          <section className="overall-status" id="overview" aria-label="整体概览">
            <h2>整体概览</h2>
            <div className="overall-pills">
              <span className="overall-pill pill-total">监测站点 {overviewData.length}</span>
              <span className="overall-pill pill-normal">正常 {statusCounts['正常'] || 0}</span>
              <span className="overall-pill pill-attention">注意 {statusCounts['注意'] || 0}</span>
              <span className="overall-pill pill-warning">警戒 {statusCounts['警戒'] || 0}</span>
              <span className="overall-pill pill-danger">超警 {statusCounts['超警'] || 0}</span>
              <span className="overall-pill pill-alert">待处理预警 {activeWarnTotal}</span>
            </div>
          </section>
        )}

        {overviewData.length > 0 && (
          <section className="overview-section" aria-label="水文站总览">
            <h2>各站点状态</h2>
            <div className="overview-grid">
              {overviewData.map((item) => {
                const st = item.status || '正常'
                return (
                  <article
                    key={item.station_id}
                    className={`overview-card overview-${getStatusClass(st).replace('status-', '')}`}
                    onClick={() => setSelectedStationId(item.station_id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        setSelectedStationId(item.station_id)
                      }
                    }}
                  >
                    <h3>{item.station_name}</h3>
                    <p className="overview-level">水位 {item.water_level.toFixed(2)} m</p>
                    <p className="overview-rainfall">降雨 {(item.rainfall || 0).toFixed(1)} mm</p>
                    <span className={`overview-status ${getStatusClass(st)}`}>
                      {st}
                    </span>
                  </article>
                )
              })}
            </div>
          </section>
        )}

        <section className="ai-analysis-section" id="ai" aria-label="AI 智能水情分析">
          <div className="ai-header">
            <h2>AI 智能水情分析</h2>
            <span className="ai-source-tag">分析来源：规则分析（暂未接入外部 AI 模型）</span>
          </div>
          <div className="ai-actions">
            <button
              type="button"
              className="ai-run-btn"
              onClick={loadAiAnalysis}
              disabled={aiLoading}
            >
              {aiLoading ? '正在分析...' : '运行智能分析'}
            </button>
            <span className="ai-hint">按需生成，不随自动轮询刷新</span>
          </div>
          {aiError && (
            <p className="notice error" role="alert">
              {aiError}，请稍后重试。其他水情功能不受影响。
            </p>
          )}
          {!aiLoading && !aiError && aiAnalysis === null && (
            <p className="notice">
              点击「运行智能分析」获取当前水情风险判断、关键发现与建议。
            </p>
          )}
          {aiAnalysis && (
            <div className="ai-result">
              <div className="ai-overview-row">
                <div className={`ai-risk-pill ${aiRiskClass(aiAnalysis.risk_level)}`}>
                  <span className="ai-risk-label">AI 风险等级</span>
                  <strong className="ai-risk-value">{aiRiskLabel(aiAnalysis.risk_level)}</strong>
                  <small className="ai-risk-score">风险评分 {aiAnalysis.risk_score || 0} / 100</small>
                </div>
                <div className="ai-summary-wrap">
                  <p className="ai-summary">{aiAnalysis.summary || '暂无总体判断'}</p>
                  {aiAnalysis.analysis_source && (
                    <p className="ai-source-note">
                      分析来源：{aiAnalysis.analysis_source === 'rule_based' ? '规则分析（暂未接入外部 AI 模型）' : aiAnalysis.analysis_source}
                      {aiAnalysis.generated_at ? `　生成时间：${formatUpdateTime(aiAnalysis.generated_at)}` : ''}
                    </p>
                  )}
                  {aiAnalysis.note && <p className="ai-note">{aiAnalysis.note}</p>}
                </div>
              </div>

              <div className="ai-grid">
                <div className="ai-block">
                  <h3>关键发现</h3>
                  {(Array.isArray(aiAnalysis.key_findings) ? aiAnalysis.key_findings : []).length > 0 ? (
                    <ul className="ai-list">
                      {(Array.isArray(aiAnalysis.key_findings) ? aiAnalysis.key_findings : []).map((f, i) => (
                        <li key={i}>{f}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="ai-empty">暂无关键发现</p>
                  )}
                </div>
                <div className="ai-block">
                  <h3>趋势分析</h3>
                  <p className="ai-overall-trend">
                    {aiAnalysis.trend_analysis && aiAnalysis.trend_analysis.overall}
                  </p>
                  {(Array.isArray(aiAnalysis.trend_analysis && aiAnalysis.trend_analysis.stations) ? aiAnalysis.trend_analysis.stations : []).length > 0 ? (
                    <ul className="ai-list">
                      {aiAnalysis.trend_analysis.stations.slice(0, 5).map((t, i) => (
                        <li key={t.station_id || i}>{t.station_name}：{t.description}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="ai-empty">暂无趋势数据</p>
                  )}
                </div>
              </div>

              <div className="ai-grid">
                <div className="ai-block">
                  <h3>重点关注站点</h3>
                  {(Array.isArray(aiAnalysis.abnormal_stations) ? aiAnalysis.abnormal_stations : []).length > 0 ? (
                    <ul className="ai-station-list">
                      {aiAnalysis.abnormal_stations.map((s) => (
                        <li key={s.station_id}>
                          <span className="ai-station-name">{s.station_name}</span>
                          <span className={`ai-risk-chip ${aiRiskClass(s.risk_level)}`}>{aiRiskLabel(s.risk_level)}</span>
                          <span className="ai-station-meta">水位 {Number(s.water_level).toFixed(2)} m</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="ai-empty">当前无异常关注站点</p>
                  )}
                </div>
                <div className="ai-block">
                  <h3>建议</h3>
                  {(Array.isArray(aiAnalysis.recommendations) ? aiAnalysis.recommendations : []).length > 0 ? (
                    <ul className="ai-list">
                      {aiAnalysis.recommendations.map((r, i) => (
                        <li key={i}>{r}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="ai-empty">暂无建议</p>
                  )}
                </div>
              </div>
            </div>
          )}
        </section>

        <StationMap
          stations={stations}
          overviewData={overviewData}
          selectedStationId={selectedStationId}
          onStationSelect={setSelectedStationId}
          latestWarnings={latestWarnings}
        />

        {isLoading && <p className="notice loading">正在获取水情数据...</p>}

        {errorMessage && (
          <section className="notice error" role="alert">
            <strong>{errorMessage}</strong>
            <span>请确认 FastAPI 后端正在运行。</span>
          </section>
        )}

        {dataErrorMessage && (
          <section className="notice data-error" role="alert">
            <strong>{dataErrorMessage}</strong>
            <span>未使用本次异常数据更新水情状态和趋势图。</span>
          </section>
        )}

        {waterData && (
          <>
            <p className="station-name">水文站：{waterData.station_name}</p>
            <section className="data-source-row" id="current" aria-label="数据来源与质量">
              <span className={`source-badge source-${String(waterData.source || 'mock').replace(/_/g, '-')}`}>
                数据来源：{sourceText(waterData.source)}
              </span>
              <span className={`quality-badge quality-${String(waterData.data_quality || 'valid').toLowerCase()}`}>
                数据质量：{qualityText(waterData.data_quality)}
              </span>
              {waterData.updated_at && (
                <span className="update-time">更新：{formatUpdateTime(waterData.updated_at)}</span>
              )}
            </section>
            <section className="data-cards" aria-label="当前水情数据">
              <article className="data-card water-level-card">
                <span>当前水位</span>
                <strong>{waterData.water_level.toFixed(2)} <small>m</small></strong>
              </article>
              <article className="data-card">
                <span>警戒水位</span>
                <strong>{waterData.warning_level.toFixed(2)} <small>m</small></strong>
              </article>
              <article className="data-card rainfall-card">
                <span>当前降雨量</span>
                <strong>{(waterData.rainfall || 0).toFixed(1)} <small>mm</small></strong>
              </article>
              <article className={`data-card status-card ${getStatusClass(status)}`}>
                <span>综合状态</span>
                <strong>{status}</strong>
              </article>
            </section>

            <section className={`warning-panel warning-${getStatusClass(status).replace('status-', '')}`}>
              <h2>综合预警</h2>
              <div className="warning-grid">
                <div className="warning-item">
                  <span className="warning-label">当前水位</span>
                  <strong className="warning-value">{waterData.water_level.toFixed(2)} m</strong>
                </div>
                <div className="warning-item">
                  <span className="warning-label">警戒水位</span>
                  <strong className="warning-value">{waterData.warning_level.toFixed(2)} m</strong>
                </div>
                <div className="warning-item">
                  <span className="warning-label">当前降雨</span>
                  <strong className="warning-value">{(waterData.rainfall || 0).toFixed(1)} mm</strong>
                </div>
                <div className="warning-item">
                  <span className="warning-label">综合状态</span>
                  <strong className={`warning-status ${getStatusClass(status)}`}>{status}</strong>
                </div>
              </div>
              <p className="warning-detail">
                {status === '正常' && `当前水位距警戒水位 ${levelDifference.toFixed(2)} m，降雨量较小。`}
                {status === '注意' && `水位接近警戒线或降雨量较大，请密切关注。`}
                {status === '警戒' && `水位达到警戒线或降雨量较大，需加强监测。`}
                {status === '超警' && `水位超过警戒线或降雨量极大，建议启动应急响应。`}
              </p>
            </section>

            <section className="warning-center" aria-label="综合预警中心">
              <h2>综合预警中心</h2>

              {warningSummary && (
                <div className="warning-summary-grid">
                  <div className="summary-card">
                    <span className="summary-label">今日预警</span>
                    <strong className="summary-value">{warningSummary.today_total}</strong>
                  </div>
                  <div className="summary-card summary-attention">
                    <span className="summary-label">注意</span>
                    <strong className="summary-value">{warningSummary.attention}</strong>
                  </div>
                  <div className="summary-card summary-warning">
                    <span className="summary-label">警戒</span>
                    <strong className="summary-value">{warningSummary.warning}</strong>
                  </div>
                  <div className="summary-card summary-critical">
                    <span className="summary-label">超警</span>
                    <strong className="summary-value">{warningSummary.critical}</strong>
                  </div>
                  <div className="summary-card summary-active">
                    <span className="summary-label">未处理</span>
                    <strong className="summary-value">{warningSummary.active}</strong>
                  </div>
                </div>
              )}

              {latestWarnings.length > 0 && (
                <div className="latest-warnings">
                  <h3>最新预警</h3>
                  {latestWarnings.slice(0, 3).map((w) => (
                    <div key={w.id} className={`latest-warning-item warning-type-${w.warning_type === '超警预警' ? 'critical' : w.warning_type === '警戒预警' ? 'warning' : 'attention'}`}>
                      <div className="latest-warning-header">
                        <span className="latest-warning-station">{w.station_name}</span>
                        <span className={`latest-warning-type ${getStatusClass(w.warning_type === '超警预警' ? '超警' : w.warning_type === '警戒预警' ? '警戒' : '注意')}`}>{w.warning_type}</span>
                      </div>
                      <div className="latest-warning-detail">
                        水位：{w.water_level.toFixed(2)} m | 降雨：{w.rainfall.toFixed(1)} mm
                      </div>
                      <div className="latest-warning-time">
                        {new Date(w.created_at).toLocaleString('zh-CN')}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {warnings.length > 0 && (
                <div className="warning-history">
                  <h3>预警历史</h3>
                  <div className="warning-table-wrapper">
                    <table className="warning-table">
                      <thead>
                        <tr>
                          <th>时间</th>
                          <th>水文站</th>
                          <th>水位</th>
                          <th>警戒</th>
                          <th>降雨</th>
                          <th>等级</th>
                          <th>状态</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {warnings.map((w) => (
                          <tr key={w.id}>
                            <td>{new Date(w.created_at).toLocaleString('zh-CN')}</td>
                            <td>{w.station_name}</td>
                            <td>{w.water_level.toFixed(2)} m</td>
                            <td>{w.warning_level.toFixed(2)} m</td>
                            <td>{w.rainfall.toFixed(1)} mm</td>
                            <td>
                              <span className={`warning-type-badge ${getStatusClass(w.warning_type === '超警预警' ? '超警' : w.warning_type === '警戒预警' ? '警戒' : '注意')}`}>
                                {w.warning_type}
                              </span>
                            </td>
                            <td>{w.is_handled ? '已处理' : '未处理'}</td>
                            <td>
                              {user.role === 'admin' && !w.is_handled && (
                                <button
                                  className="handle-btn"
                                  onClick={() => handleWarningClick(w.id)}
                                >
                                  处理
                                </button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </section>

            {rainfallSummary && (
              <section className="rainfall-section" id="rainfall" aria-label="雨情监测">
                <h2>雨情监测</h2>
                <div className="rainfall-grid">
                  <div className="rainfall-stat">
                    <span className="stat-label">24小时累计降雨</span>
                    <strong className="stat-value">{rainfallSummary.last_24h_rainfall} mm</strong>
                  </div>
                  <div className="rainfall-stat">
                    <span className="stat-label">最大单次降雨</span>
                    <strong className="stat-value">{rainfallSummary.max_rainfall} mm</strong>
                  </div>
                  <div className="rainfall-stat">
                    <span className="stat-label">降雨等级</span>
                    <strong className="stat-value">{rainfallSummary.rainfall_level}</strong>
                  </div>
                </div>
              </section>
            )}
          </>
        )}

        <button type="button" onClick={() => loadWaterData(selectedStationId)} disabled={isLoading}>
          {isLoading ? '正在刷新...' : '刷新水情数据'}
        </button>
        <p className="refresh-tip">系统每 10 秒自动更新一次数据</p>

        {trendData.length > 0 && (
          <section className="trend-section" aria-label="水位变化趋势">
            <h2>水位变化趋势</h2>
            <ReactECharts option={waterChartOption} style={{ height: '340px' }} />
          </section>
        )}

        {trendData.length > 0 && (
          <section className="trend-section" aria-label="降雨量趋势">
            <h2>降雨量趋势</h2>
            <ReactECharts option={rainfallChartOption} style={{ height: '300px' }} />
          </section>
        )}

        {/* ── 第14阶段：历史数据分析与趋势预测 ── */}
        <section className="history-analysis-section" id="history" aria-label="历史分析">
          <div className="history-header">
            <h2>历史分析</h2>
            <div className="history-range-toggle">
              <button
                type="button"
                className={historyRange === '24h' ? 'active' : ''}
                onClick={() => setHistoryRange('24h')}
              >24小时</button>
              <button
                type="button"
                className={historyRange === '168h' ? 'active' : ''}
                onClick={() => setHistoryRange('168h')}
              >7天</button>
            </div>
          </div>

          {analysisLoading && (
            <p className="notice loading">正在加载历史分析数据...</p>
          )}

          {!analysisLoading && analysisData && (
            <>
              {analysisData.statistics && analysisData.statistics.sufficient && (
                <div className="history-stats-grid">
                  <div className="stat-card">
                    <span className="stat-label">最高水位</span>
                    <span className="stat-value">{analysisData.statistics.max_water_level} m</span>
                  </div>
                  <div className="stat-card">
                    <span className="stat-label">最低水位</span>
                    <span className="stat-value">{analysisData.statistics.min_water_level} m</span>
                  </div>
                  <div className="stat-card">
                    <span className="stat-label">平均水位</span>
                    <span className="stat-value">{analysisData.statistics.avg_water_level} m</span>
                  </div>
                  <div className="stat-card">
                    <span className="stat-label">累计降雨</span>
                    <span className="stat-value">{analysisData.statistics.total_rainfall} mm</span>
                  </div>
                  <div className="stat-card">
                    <span className="stat-label">预警次数</span>
                    <span className="stat-value">{analysisData.statistics.warning_count} 次</span>
                  </div>
                </div>
              )}

              {!analysisData.statistics || !analysisData.statistics.sufficient ? (
                <p className="notice insufficient">历史数据不足，暂无法生成统计分析</p>
              ) : null}

              {analysisData.trend && analysisData.trend.sufficient && (
                <div className={`trend-analysis-card trend-${analysisData.trend.trend}`}>
                  <div className="trend-badge-row">
                    <span className="stat-label">水位趋势</span>
                    <span className={`trend-badge trend-badge-${analysisData.trend.trend}`}>
                      {analysisData.trend.trend === 'rising' ? '↑ 上升'
                        : analysisData.trend.trend === 'falling' ? '↓ 下降'
                        : '→ 平稳'}
                    </span>
                    <span className="trend-change">
                      变化量：{analysisData.trend.change >= 0 ? '+' : ''}{analysisData.trend.change} m
                    </span>
                  </div>
                  <p className="trend-desc">{analysisData.trend.trend_description}</p>
                </div>
              )}

              {analysisData.trend && !analysisData.trend.sufficient && (
                <p className="notice insufficient">历史数据不足，暂无法进行趋势分析</p>
              )}

              {analysisData.forecast && analysisData.forecast.sufficient && (
                <div className="forecast-card">
                  <h3>趋势预测</h3>
                  <div className="forecast-grid">
                    <div className="forecast-item">
                      <span className="stat-label">当前水位</span>
                      <span className="stat-value">{analysisData.forecast.current_water_level} m</span>
                    </div>
                    <div className="forecast-item">
                      <span className="stat-label">预测水位</span>
                      <span className="stat-value">{analysisData.forecast.predicted_water_level} m</span>
                    </div>
                    <div className="forecast-item">
                      <span className="stat-label">趋势</span>
                      <span className={`trend-badge trend-badge-${analysisData.forecast.trend}`}>
                        {analysisData.forecast.trend === 'rising' ? '↑ 上升'
                          : analysisData.forecast.trend === 'falling' ? '↓ 下降'
                          : '→ 平稳'}
                      </span>
                    </div>
                    <div className="forecast-item">
                      <span className="stat-label">风险等级</span>
                      <span className={`risk-badge risk-${analysisData.forecast.risk_level}`}>
                        {analysisData.forecast.risk_level === 'danger' ? '红色预警'
                          : analysisData.forecast.risk_level === 'warning' ? '橙色预警'
                          : analysisData.forecast.risk_level === 'attention' ? '黄色预警'
                          : '正常'}
                      </span>
                    </div>
                  </div>
                  <p className="forecast-note">{analysisData.forecast.message}</p>
                </div>
              )}

              {analysisData.forecast && !analysisData.forecast.sufficient && (
                <p className="notice insufficient">历史数据不足，暂无法进行趋势预测</p>
              )}

              {analysisData.history && analysisData.history.data && analysisData.history.data.length > 0 && (
                <div className="history-charts">
                  <div className="history-chart-container">
                    <h3>水位变化</h3>
                    <ReactECharts
                      option={{
                        tooltip: { trigger: 'axis' },
                        grid: { left: 55, right: 30, top: 45, bottom: 50 },
                        dataZoom: [{ type: 'inside', start: 0, end: 100 }],
                        xAxis: {
                          type: 'category',
                          name: '时间',
                          axisLabel: {
                            rotate: historyRange === '168h' ? 35 : 0,
                            formatter: (val) => {
                              const d = new Date(val)
                              return historyRange === '168h'
                                ? `${(d.getMonth() + 1).toString().padStart(2, '0')}/${d.getDate().toString().padStart(2, '0')} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
                                : `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
                            },
                          },
                          data: analysisData.history.data.map(d => d.timestamp),
                        },
                        yAxis: { type: 'value', name: '水位（m）', axisLabel: { formatter: '{value} m' } },
                        series: [{
                          name: '水位',
                          type: 'line',
                          data: analysisData.history.data.map(d => d.water_level),
                          smooth: true,
                          symbolSize: 6,
                          lineStyle: { color: '#0b5d74', width: 2 },
                          itemStyle: { color: '#0b5d74' },
                          areaStyle: { color: 'rgba(11, 93, 116, 0.08)' },
                          markLine: analysisData.statistics && analysisData.statistics.sufficient
                            ? {
                                symbol: 'none',
                                lineStyle: { color: '#d97706', type: 'dashed' },
                                label: { formatter: '平均水位' },
                                data: [{ yAxis: analysisData.statistics.avg_water_level }],
                              }
                            : undefined,
                        }],
                      }}
                      style={{ height: '320px' }}
                    />
                  </div>
                  <div className="history-chart-container">
                    <h3>降雨分布</h3>
                    <ReactECharts
                      option={{
                        tooltip: { trigger: 'axis' },
                        grid: { left: 55, right: 30, top: 45, bottom: 50 },
                        dataZoom: [{ type: 'inside', start: 0, end: 100 }],
                        xAxis: {
                          type: 'category',
                          name: '时间',
                          axisLabel: {
                            rotate: historyRange === '168h' ? 35 : 0,
                            formatter: (val) => {
                              const d = new Date(val)
                              return historyRange === '168h'
                                ? `${(d.getMonth() + 1).toString().padStart(2, '0')}/${d.getDate().toString().padStart(2, '0')} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
                                : `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
                            },
                          },
                          data: analysisData.history.data.map(d => d.timestamp),
                        },
                        yAxis: { type: 'value', name: '降雨量（mm）', axisLabel: { formatter: '{value} mm' } },
                        series: [{
                          name: '降雨量',
                          type: 'bar',
                          data: analysisData.history.data.map(d => ({
                            value: d.rainfall,
                            itemStyle: {
                              color: d.rainfall >= 50 ? '#dc2626'
                                : d.rainfall >= 30 ? '#ea580c'
                                : d.rainfall >= 15 ? '#d97706'
                                : d.rainfall >= 5 ? '#2563eb'
                                : '#94a3b8',
                            },
                          })),
                          barMaxWidth: 20,
                        }],
                      }}
                      style={{ height: '300px' }}
                    />
                  </div>
                </div>
              )}
            </>
          )}
        </section>

        {/* ── 第15阶段：多站综合对比 ── */}
        <section className="comparison-section" id="compare" aria-label="多站综合对比">
          <div className="history-header">
            <h2>多站综合对比</h2>
            <div className="history-range-toggle">
              <button
                type="button"
                className={comparisonRange === '24h' ? 'active' : ''}
                onClick={() => setComparisonRange('24h')}
              >24小时</button>
              <button
                type="button"
                className={comparisonRange === '168h' ? 'active' : ''}
                onClick={() => setComparisonRange('168h')}
              >7天</button>
            </div>
          </div>

          {comparisonLoading && (
            <p className="notice loading">正在加载多站对比数据...</p>
          )}

          {!comparisonLoading && comparisonData && comparisonData.sufficient && (
            <>
              <div className="highlight-cards">
                <button
                  type="button"
                  className="highlight-card"
                  onClick={() => comparisonData.highest_water_level_station && setSelectedStationId(comparisonData.highest_water_level_station.station_id)}
                >
                  <span className="highlight-label">当前最高水位</span>
                  <strong>{comparisonData.highest_water_level_station ? `${comparisonData.highest_water_level_station.current_water_level} m` : '数据不足'}</strong>
                  <small>{comparisonData.highest_water_level_station ? comparisonData.highest_water_level_station.station_name : '点击切换'}</small>
                </button>
                <button
                  type="button"
                  className="highlight-card"
                  onClick={() => comparisonData.fastest_rising_station && setSelectedStationId(comparisonData.fastest_rising_station.station_id)}
                >
                  <span className="highlight-label">水位上涨最快</span>
                  <strong>{comparisonData.fastest_rising_station ? `${comparisonData.fastest_rising_station.rate_per_hour} m/h` : '数据不足'}</strong>
                  <small>{comparisonData.fastest_rising_station ? comparisonData.fastest_rising_station.station_name : '点击切换'}</small>
                </button>
                <button
                  type="button"
                  className="highlight-card"
                  onClick={() => comparisonData.highest_rainfall_station && setSelectedStationId(comparisonData.highest_rainfall_station.station_id)}
                >
                  <span className="highlight-label">降雨最高</span>
                  <strong>{comparisonData.highest_rainfall_station ? `${comparisonData.highest_rainfall_station.total_rainfall} mm` : '数据不足'}</strong>
                  <small>{comparisonData.highest_rainfall_station ? comparisonData.highest_rainfall_station.station_name : '点击切换'}</small>
                </button>
                <button
                  type="button"
                  className="highlight-card highlight-risk"
                  onClick={() => comparisonData.highest_risk_station && setSelectedStationId(comparisonData.highest_risk_station.station_id)}
                >
                  <span className="highlight-label">当前风险最高</span>
                  <strong>{comparisonData.highest_risk_station ? riskLevelLabel(comparisonData.highest_risk_station.risk_level) : '数据不足'}</strong>
                  <small>{comparisonData.highest_risk_station ? comparisonData.highest_risk_station.station_name : '点击切换'}</small>
                </button>
              </div>

              <div className="comparison-charts">
                <div className="history-chart-container">
                  <h3>水位对比</h3>
                  <ReactECharts option={waterCompareOption(comparisonData, comparisonRange)} style={{ height: '340px' }} />
                </div>
                <div className="history-chart-container">
                  <h3>降雨对比</h3>
                  <ReactECharts option={rainfallCompareOption(comparisonData)} style={{ height: '300px' }} />
                </div>
              </div>

              <div className="risk-ranking-block">
                <h3>站点风险排名</h3>
                <table className="risk-table">
                  <thead>
                    <tr>
                      <th>排名</th>
                      <th>站点</th>
                      <th>当前水位</th>
                      <th>警戒水位</th>
                      <th>风险等级</th>
                      <th>趋势</th>
                    </tr>
                  </thead>
                  <tbody>
                    {comparisonData.risk_ranking.map((s, index) => (
                      <tr
                        key={s.station_id}
                        onClick={() => setSelectedStationId(s.station_id)}
                        className={selectedStationId === s.station_id ? 'active-row' : ''}
                      >
                        <td>{index + 1}</td>
                        <td>{s.station_name}</td>
                        <td>{s.sufficient ? `${s.current_water_level} m` : '历史数据不足'}</td>
                        <td>{s.warning_level} m</td>
                        <td>
                          {s.sufficient ? (
                            <span className={`risk-badge risk-${s.risk_level}`}>{riskLevelLabel(s.risk_level)}</span>
                          ) : '—'}
                        </td>
                        <td>
                          {s.sufficient ? (
                            <span className={`trend-badge trend-badge-${s.water_level_trend}`}>{trendLabel(s.water_level_trend)}</span>
                          ) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {!comparisonLoading && comparisonData && !comparisonData.sufficient && (
            <p className="notice insufficient">历史数据不足，暂无法进行多站对比分析</p>
          )}
        </section>

        {/* ── 第16阶段：数据报表 ── */}
        <section className="report-section" id="report" aria-label="数据报表">
          <div className="report-header">
            <h2>数据报表</h2>
            <div className="report-actions">
              <div className="report-range-toggle">
                <button
                  type="button"
                  className={reportRange === '24h' ? 'active' : ''}
                  onClick={() => setReportRange('24h')}
                >24小时</button>
                <button
                  type="button"
                  className={reportRange === '7d' ? 'active' : ''}
                  onClick={() => setReportRange('7d')}
                >7天</button>
                <button
                  type="button"
                  className={reportRange === '30d' ? 'active' : ''}
                  onClick={() => setReportRange('30d')}
                >30天</button>
              </div>
              <select
                className="report-station-select"
                value={reportStation}
                onChange={(e) => setReportStation(e.target.value)}
              >
                <option value="">全部站点</option>
                {stations.map((s) => (
                  <option key={s.station_id} value={s.station_id}>
                    {s.station_name}
                  </option>
                ))}
              </select>
              <div className="report-export-buttons">
                <button type="button" className="report-export-btn" onClick={() => downloadReport('csv')}>
                  导出 CSV
                </button>
                <button type="button" className="report-export-btn" onClick={() => downloadReport('excel')}>
                  导出 Excel
                </button>
              </div>
            </div>
          </div>

          {reportLoading && (
            <p className="notice loading">正在加载报表数据...</p>
          )}

          {!reportLoading && reportStats && (
            <div className="report-stats-grid">
              <div className="stat-card">
                <span className="stat-label">平均水位</span>
                <span className="stat-value">{reportStats.avg_water_level !== null && reportStats.avg_water_level !== undefined ? `${reportStats.avg_water_level} m` : '—'}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">最高水位</span>
                <span className="stat-value">{reportStats.max_water_level !== null && reportStats.max_water_level !== undefined ? `${reportStats.max_water_level} m` : '—'}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">最低水位</span>
                <span className="stat-value">{reportStats.min_water_level !== null && reportStats.min_water_level !== undefined ? `${reportStats.min_water_level} m` : '—'}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">平均降雨量</span>
                <span className="stat-value">{reportStats.avg_rainfall !== null && reportStats.avg_rainfall !== undefined ? `${reportStats.avg_rainfall} mm` : '—'}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">最大降雨量</span>
                <span className="stat-value">{reportStats.max_rainfall !== null && reportStats.max_rainfall !== undefined ? `${reportStats.max_rainfall} mm` : '—'}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">预警次数</span>
                <span className="stat-value">{reportStats.warning_count} 次</span>
              </div>
            </div>
          )}

          {!reportLoading && !reportStats && (
            <p className="notice error">报表数据加载失败，请稍后重试。</p>
          )}

          {!reportLoading && reportData && (
            <div className="report-table-wrapper">
              {reportData.data && reportData.data.length > 0 ? (
                <table className="report-table">
                  <thead>
                    <tr>
                      <th>站点</th>
                      <th>采集时间</th>
                      <th>水位</th>
                      <th>警戒水位</th>
                      <th>降雨量</th>
                      <th>状态</th>
                      <th>数据来源</th>
                      <th>数据质量</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reportData.data.map((r, index) => (
                      <tr key={`${r.station_id}-${r.timestamp}-${index}`}>
                        <td>{r.station_name}</td>
                        <td>{new Date(r.timestamp).toLocaleString('zh-CN')}</td>
                        <td>{r.water_level} m</td>
                        <td>{r.warning_level} m</td>
                        <td>{r.rainfall} mm</td>
                        <td>
                          <span className={`status-badge ${getStatusClass(r.status)}`}>{r.status}</span>
                        </td>
                        <td>{sourceText(r.source)}</td>
                        <td>{qualityText(r.data_quality)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="notice insufficient">当前时间范围内暂无报表数据</p>
              )}
            </div>
          )}
        </section>

      {/* ── 第17阶段：智能预警中心 ── */}
        <section className="alert-center" id="alerts" aria-label="智能预警中心">
          <div className="alert-header">
            <h2>智能预警中心</h2>
            <div className="alert-filters">
              <select
                className="alert-filter-station"
                value={alertStation}
                onChange={(e) => setAlertStation(e.target.value)}
              >
                <option value="">全部站点</option>
                {stations.map((s) => (
                  <option key={s.station_id} value={s.station_id}>
                    {s.station_name}
                  </option>
                ))}
              </select>
              <select
                className="alert-filter-level"
                value={alertLevel}
                onChange={(e) => setAlertLevel(e.target.value)}
              >
                <option value="">全部等级</option>
                <option value="attention">注意</option>
                <option value="warning">预警</option>
                <option value="danger">危险</option>
              </select>
              <select
                className="alert-filter-status"
                value={alertStatus}
                onChange={(e) => setAlertStatus(e.target.value)}
              >
                <option value="">全部状态</option>
                <option value="pending">待处理</option>
                <option value="acknowledged">已确认</option>
                <option value="resolved">已解除</option>
              </select>
              <div className="alert-range-toggle">
                <button
                  type="button"
                  className={alertRange === '24h' ? 'active' : ''}
                  onClick={() => setAlertRange('24h')}
                >24小时</button>
                <button
                  type="button"
                  className={alertRange === '7d' ? 'active' : ''}
                  onClick={() => setAlertRange('7d')}
                >7天</button>
                <button
                  type="button"
                  className={alertRange === '30d' ? 'active' : ''}
                  onClick={() => setAlertRange('30d')}
                >30天</button>
              </div>
            </div>
          </div>

          {alertSummary && (
            <div className="alert-stats-grid">
              <div className="stat-card">
                <span className="stat-label">预警总数</span>
                <span className="stat-value">{alertSummary.total}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">待处理</span>
                <span className="stat-value">{alertSummary.pending}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">已确认</span>
                <span className="stat-value">{alertSummary.acknowledged}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">已解除</span>
                <span className="stat-value">{alertSummary.resolved}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">注意</span>
                <span className="stat-value">{alertSummary.attention}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">预警</span>
                <span className="stat-value">{alertSummary.warning}</span>
              </div>
              <div className="stat-card">
                <span className="stat-label">危险</span>
                <span className="stat-value">{alertSummary.danger}</span>
              </div>
            </div>
          )}

          {alertLoading && (
            <p className="notice loading">正在加载预警数据...</p>
          )}

          {!alertLoading && (!alertItems || alertItems.length === 0) && (
            <p className="notice insufficient">当前筛选范围内暂无预警记录</p>
          )}

          {!alertLoading && alertItems && alertItems.length > 0 && (
            <div className="alert-cards">
              {alertItems.map((a) => (
                <article
                  key={a.id}
                  className={`alert-card alert-level-${a.warning_level}`}
                >
                  <div
                    className="alert-card-top"
                    role="button"
                    tabIndex={0}
                    onClick={() => setAlertExpandedId(alertExpandedId === a.id ? null : a.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        setAlertExpandedId(alertExpandedId === a.id ? null : a.id)
                      }
                    }}
                  >
                    <div className="alert-card-left">
                      <span className={`alert-level alert-level-${a.warning_level}`}>
                        {ALERT_LEVEL_LABELS[a.warning_level] || a.warning_level}
                      </span>
                      <span className="alert-title">
                        {a.title || `${a.station_name}水情预警`}
                      </span>
                    </div>
                    <span className={`alert-status alert-status-${a.status}`}>
                      {ALERT_STATUS_LABELS[a.status] || a.status}
                    </span>
                  </div>
                  <div className="alert-card-meta">
                    <span>站点：{a.station_name}</span>
                    <button
                      type="button"
                      className="alert-station-link"
                      onClick={() => setSelectedStationId(a.station_id)}
                    >
                      查看站点
                    </button>
                  </div>
                  {alertExpandedId === a.id && (
                    <div className="alert-detail">
                      <p className="alert-message">{a.message}</p>
                      <div className="alert-metrics">
                        <span>当前水位 <strong>{Number(a.water_level).toFixed(2)} m</strong></span>
                        <span>警戒水位 <strong>{Number(a.warning_level_value).toFixed(2)} m</strong></span>
                        <span>降雨量 <strong>{Number(a.rainfall).toFixed(1)} mm</strong></span>
                        <span>触发时间 <strong>{new Date(a.created_at).toLocaleString('zh-CN')}</strong></span>
                      </div>
                      {(a.acknowledged_at || a.resolved_at) && (
                        <div className="alert-history">
                          {a.acknowledged_at && (
                            <span>确认：{new Date(a.acknowledged_at).toLocaleString('zh-CN')}（{a.acknowledged_by || '—'}）</span>
                          )}
                          {a.resolved_at && (
                            <span>解除：{new Date(a.resolved_at).toLocaleString('zh-CN')}（{a.resolved_by || '—'}）</span>
                          )}
                        </div>
                      )}
                      {user.role === 'admin' && a.status !== 'resolved' && (
                        <div className="alert-actions">
                          {a.status !== 'acknowledged' && (
                            <button
                              type="button"
                              className="alert-btn alert-btn-acknowledge"
                              onClick={() => handleAlertAcknowledge(a.id)}
                            >
                              确认预警
                            </button>
                          )}
                          <button
                            type="button"
                            className="alert-btn alert-btn-resolve"
                            onClick={() => handleAlertResolve(a.id)}
                          >
                            解除预警
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </article>
              ))}
            </div>
          )}
        </section>

      </section>
    </main>
  )
}

function App() {
  const [user, setUser] = useState(null)
  const [token, setToken] = useState(null)
  const [checkingAuth, setCheckingAuth] = useState(true)
  const [registerMode, setRegisterMode] = useState(false)
  const [loginNotice, setLoginNotice] = useState('')

  useEffect(() => {
    const savedToken = localStorage.getItem('token')
    const savedUser = localStorage.getItem('user')
    if (savedToken && savedUser) {
      try {
        setToken(savedToken)
        setUser(JSON.parse(savedUser))
      } catch {
        localStorage.removeItem('token')
        localStorage.removeItem('user')
      }
    }
    setCheckingAuth(false)
  }, [])

  useEffect(() => {
    if (!token) return
    const checkToken = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/auth/me`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!response.ok) {
          handleLogout()
        }
      } catch {
        // keep token if server unreachable
      }
    }
    checkToken()
    const handleFocus = () => checkToken()
    window.addEventListener('focus', handleFocus)
    return () => window.removeEventListener('focus', handleFocus)
  }, [token])

  const handleLogin = (accessToken, userData) => {
    setToken(accessToken)
    setUser(userData)
    localStorage.setItem('token', accessToken)
    localStorage.setItem('user', JSON.stringify(userData))
  }

  const handleLogout = () => {
    setToken(null)
    setUser(null)
    localStorage.removeItem('token')
    localStorage.removeItem('user')
  }

  if (checkingAuth) {
    return (
      <div className="login-page">
        <div className="login-container">
          <p className="login-subtitle">加载中...</p>
        </div>
      </div>
    )
  }

  if (!user || !token) {
    if (registerMode) {
      return (
        <RegisterPage
          onBack={() => {
            setRegisterMode(false)
            setLoginNotice('')
          }}
          onRegistered={() => {
            setRegisterMode(false)
            setLoginNotice('注册成功，请登录')
          }}
        />
      )
    }
    return (
      <LoginPage
        onLogin={handleLogin}
        onRegister={() => setRegisterMode(true)}
        notice={loginNotice}
      />
    )
  }

  return <MonitorApp user={user} token={token} onLogout={handleLogout} />
}

export default App
