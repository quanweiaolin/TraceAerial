import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { FolderAuditPage } from "./pages/FolderAuditPage";
import { LoginPage } from "./pages/LoginPage";
import { QueuePage } from "./pages/QueuePage";
import { ReviewDetailPage } from "./pages/ReviewDetailPage";

function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<AppShell />}>
        <Route index element={<QueuePage />} />
        <Route path="queue" element={<QueuePage />} />
        <Route path="queue/:itemId" element={<ReviewDetailPage />} />
        <Route path="folder-audit" element={<FolderAuditPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default App;
