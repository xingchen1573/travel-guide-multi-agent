import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { ProjectListPage } from './pages/ProjectListPage'
import { TrashPage } from './pages/TrashPage'
import { ProjectOverviewPage } from './pages/ProjectOverviewPage'
import { AgentOutputsPage } from './pages/AgentOutputsPage'
import { PoiPhotosPage } from './pages/PoiPhotosPage'
import { UserNotesPage } from './pages/UserNotesPage'
import { TimelinePage } from './pages/TimelinePage'
import { MaterialsPage } from './pages/MaterialsPage'
import { MaterialInsightsPage } from './pages/MaterialInsightsPage'
import { PresentationPage } from './pages/PresentationPage'
import { ModelConfigPage } from './pages/ModelConfigPage'
import { FeishuOpsPage } from './pages/FeishuOpsPage'
import { ReviewCenterPage } from './pages/ReviewCenterPage'
import { ExperimentsPage } from './pages/ExperimentsPage'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<ProjectListPage />} />
          <Route path="trash" element={<TrashPage />} />
          <Route path="projects/:projectId" element={<ProjectOverviewPage />} />
          <Route path="projects/:projectId/outputs" element={<AgentOutputsPage />} />
          <Route path="projects/:projectId/poi_photos" element={<PoiPhotosPage />} />
          <Route path="projects/:projectId/user_notes" element={<UserNotesPage />} />
          <Route path="projects/:projectId/timeline" element={<TimelinePage />} />
          <Route path="projects/:projectId/presentation" element={<PresentationPage />} />
          <Route path="materials" element={<MaterialsPage />} />
          <Route path="materials/insights" element={<MaterialInsightsPage />} />
          <Route path="model-config" element={<ModelConfigPage />} />
          <Route path="feishu" element={<FeishuOpsPage />} />
          <Route path="review-center" element={<ReviewCenterPage />} />
          <Route path="experiments" element={<ExperimentsPage />} />
          <Route path="projects/:projectId/review" element={<ReviewCenterPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
