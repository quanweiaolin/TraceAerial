import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { AgreementRatePage } from "./pages/AgreementRatePage";
import { FolderAuditPage } from "./pages/FolderAuditPage";
import { GroupsPage } from "./pages/GroupsPage";
import { LocationsPage } from "./pages/LocationsPage";
import { LoginPage } from "./pages/LoginPage";
import { NamingAuditPage } from "./pages/NamingAuditPage";
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
        <Route path="locations" element={<LocationsPage />} />
        <Route path="groups" element={<GroupsPage />} />
        <Route path="naming-audit" element={<NamingAuditPage />} />
        <Route path="agreement-rate" element={<AgreementRatePage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default App;
