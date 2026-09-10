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
    <section className="map-section" aria-label="水文站地图">
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

function LoginPage({ onLogin }) {
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
      </div>
    </div>
  )
}

function UserManager({ token, onBack }) {
  const [users, setUsers] = useState([])
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState('user')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const loadUsers = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/users`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (response.ok) {
        const result = await response.json()
        setUsers(result.data)
      }
    } catch {
      // ignore
    }
  }, [token])

  useEffect(() => {
    loadUsers()
  }, [loadUsers])

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
      if (response.ok) {
        await loadUsers()
      }
    } catch {
      // ignore
    }
  }

  return (
    <div className="user-manager-page">
      <div className="user-manager-container">
        <div className="user-manager-header">
          <h1>用户管理</h1>
          <button className="back-btn" onClick={onBack}>返回监测大屏</button>
        </div>

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
    </div>
  )
}

function MonitorApp({ user, onLogout }) {
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
  const [showUserManager, setShowUserManager] = useState(false)
  const timerRef = useRef(null)

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
      await loadHistoryData(stationId)
      await loadOverview()
      await loadRainfallSummary(stationId)
      await loadWarnings()
      await loadWarningSummary()
      await loadLatestWarnings()
    } catch {
      setErrorMessage('无法连接水情监测服务器')
    } finally {
      setIsLoading(false)
    }
  }, [loadHistoryData, loadOverview, loadRainfallSummary, loadWarnings, loadWarningSummary, loadLatestWarnings])

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

  const handleStationChange = (e) => {
    setSelectedStationId(e.target.value)
  }

  const status = waterData ? waterData.status : '正常'
  const levelDifference = waterData
    ? Math.abs(waterData.water_level - waterData.warning_level)
    : 0

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

  if (showUserManager) {
    return <UserManager token={localStorage.getItem('token')} onBack={() => setShowUserManager(false)} />
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
              {user.role === 'admin' && (
                <button className="admin-btn" onClick={() => setShowUserManager(true)}>用户管理</button>
              )}
              <button className="logout-btn" onClick={onLogout}>退出</button>
            </div>
          </div>
        </header>

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
          <section className="overview-section" aria-label="水文站总览">
            <h2>水文站总览</h2>
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
                              {!w.is_handled && (
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
              <section className="rainfall-section" aria-label="雨情监测">
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
      </section>
    </main>
  )
}

function App() {
  const [user, setUser] = useState(null)
  const [token, setToken] = useState(null)
  const [checkingAuth, setCheckingAuth] = useState(true)

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
    return <LoginPage onLogin={handleLogin} />
  }

  return <MonitorApp user={user} onLogout={handleLogout} />
}

export default App
