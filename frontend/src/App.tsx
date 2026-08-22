import { Navigate, Route, Routes } from "react-router-dom";
import Studio from "./pages/Studio";
import TerminalView from "./pages/TerminalView";
import WorkspaceView from "./pages/WorkspaceView";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Studio />} />
      <Route path="/ws/:id" element={<WorkspaceView />} />
      <Route path="/term/:id" element={<TerminalView />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
