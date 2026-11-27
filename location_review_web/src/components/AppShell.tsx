import { Navigate, NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

const NAV_ITEMS = [
  { to: "/queue", label: "队列" },
  { to: "/folder-audit", label: "文件夹审查" },
  { to: "/locations", label: "地点" },
  { to: "/groups", label: "分组" },
  { to: "/naming-audit", label: "命名审核" },
  { to: "/agreement-rate", label: "一致率看板" },
];

export function AppShell() {
  const { token, logout } = useAuth();

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  return (
    <div style={{ display: "flex", minHeight: "100vh" }}>
      <nav
        style={{
          width: 200,
          borderRight: "1px solid #ddd",
          padding: 16,
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}
      >
        <h2 style={{ fontSize: 16, marginBottom: 8 }}>地址复核</h2>
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            style={({ isActive }) => ({
              fontWeight: isActive ? "bold" : "normal",
            })}
          >
            {item.label}
          </NavLink>
        ))}
        <button type="button" onClick={logout} style={{ marginTop: "auto" }}>
          退出登录
        </button>
      </nav>
      <main style={{ flex: 1, padding: 24 }}>
        <Outlet />
      </main>
    </div>
  );
}

export default AppShell;
