import { useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  useDeleteOverviewPhoto,
  useDeleteSegmentPhoto,
  usePoiDetails,
  useUploadOverviewPhoto,
  useUploadSegmentPhoto,
} from '../api/hooks'
import { api } from '../api/client'
import { Card } from '../components/Card'
import { Badge } from '../components/Badge'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { ProjectTabs } from '../components/ProjectTabs'
import type { PoiDetailFile, RouteSegment } from '../api/types'

export function PoiPhotosPage() {
  const { projectId = '' } = useParams()
  const { data, isLoading, error } = usePoiDetails(projectId)

  if (isLoading) return <p className="text-ink-3">加载中…</p>
  if (error) return <p className="text-danger">加载失败：{(error as Error).message}</p>

  return (
    <div>
      <ProjectTabs projectId={projectId} />
      {!data || data.length === 0 ? (
        <p className="text-ink-3">这个project还没有生成POI详情（outputs/poi_details/），暂时没有可上传照片的位置。</p>
      ) : (
        <div className="flex flex-col gap-4">
          {data
            .slice()
            .sort((a, b) => a.day - b.day)
            .map((detail) => (
              <PoiCard key={`${detail.day}-${detail.poi_name}`} projectId={projectId} detail={detail} />
            ))}
        </div>
      )}
    </div>
  )
}

function PoiCard({ projectId, detail }: { projectId: string; detail: PoiDetailFile }) {
  const uploadOverview = useUploadOverviewPhoto(projectId)
  const deleteOverview = useDeleteOverviewPhoto(projectId)

  return (
    <Card className="p-4">
      <div className="flex items-center gap-2 mb-3">
        <Badge variant="brand">Day{detail.day}</Badge>
        <b className="text-[15px]">{detail.poi_name}</b>
      </div>

      <div className="mb-4">
        <div className="text-[12.5px] text-ink-2 mb-1.5">POI 整体照片（树木页英雄区）</div>
        <PhotoSlot
          previewUrl={
            detail.uploaded_overview_photo_path
              ? api.overviewPhotoUrl(projectId, detail.day, detail.poi_name, detail.updated_at)
              : null
          }
          onUpload={(file) => uploadOverview.mutate({ day: detail.day, poiName: detail.poi_name, file })}
          onDelete={() => deleteOverview.mutate({ day: detail.day, poiName: detail.poi_name })}
          uploading={uploadOverview.isPending}
          error={uploadOverview.error instanceof Error ? uploadOverview.error.message : null}
        />
      </div>

      {detail.routes.map((route) => (
        <div key={route.route_name} className="mt-3 pt-3 border-t border-dashed border-border">
          <div className="text-[13px] font-semibold mb-2">
            {route.route_name}
            {route.recommended && (
              <span className="ml-1.5">
                <Badge variant="ok">推荐</Badge>
              </span>
            )}
          </div>
          <div className="flex flex-col gap-3">
            {route.route_segments
              .slice()
              .sort((a, b) => a.order - b.order)
              .map((segment) => (
                <SegmentRow
                  key={segment.order}
                  projectId={projectId}
                  day={detail.day}
                  poiName={detail.poi_name}
                  routeName={route.route_name}
                  updatedAt={detail.updated_at}
                  segment={segment}
                />
              ))}
          </div>
        </div>
      ))}
    </Card>
  )
}

function SegmentRow({
  projectId,
  day,
  poiName,
  routeName,
  updatedAt,
  segment,
}: {
  projectId: string
  day: number
  poiName: string
  routeName: string
  updatedAt: string
  segment: RouteSegment
}) {
  const uploadSegment = useUploadSegmentPhoto(projectId)
  const deleteSegment = useDeleteSegmentPhoto(projectId)

  return (
    <div className="flex items-start gap-3">
      <div className="w-6 h-6 rounded-full bg-bg-inset flex items-center justify-center text-xs font-bold shrink-0 mt-0.5">
        {segment.order}
      </div>
      <div className="flex-1">
        <div className="text-[13px] font-semibold mb-1">{segment.point_name}</div>
        <PhotoSlot
          previewUrl={
            segment.uploaded_photo_path
              ? api.segmentPhotoUrl(projectId, day, poiName, routeName, segment.order, updatedAt)
              : null
          }
          onUpload={(file) =>
            uploadSegment.mutate({ day, poiName, routeName, order: segment.order, file })
          }
          onDelete={() => deleteSegment.mutate({ day, poiName, routeName, order: segment.order })}
          uploading={uploadSegment.isPending}
          error={uploadSegment.error instanceof Error ? uploadSegment.error.message : null}
        />
      </div>
    </div>
  )
}

/** 通用的"当前预览 + 选文件上传 + 删除"槽位，POI整体照片和途经点照片共用同一套UI。
 * 原生<input type=file>，不引入react-dropzone之类的新依赖——跟这个前端其余部分的
 * "手写Tailwind + 原生表单元素"风格一致。 */
function PhotoSlot({
  previewUrl,
  onUpload,
  onDelete,
  uploading,
  error,
}: {
  previewUrl: string | null
  onUpload: (file: File) => void
  onDelete: () => void
  uploading?: boolean
  error?: string | null
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) onUpload(file)
    e.target.value = ''
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-3">
        {previewUrl ? (
          <img
            src={previewUrl}
            alt=""
            className="w-28 h-16 object-cover rounded-lg border border-border"
          />
        ) : (
          <div className="w-28 h-16 rounded-lg border border-dashed border-border-strong flex items-center justify-center text-[11px] text-ink-3">
            未上传
          </div>
        )}
        <input ref={inputRef} type="file" accept="image/*" className="hidden" onChange={handleFileChange} />
        <button
          className="text-[12px] font-semibold px-2.5 py-1.5 rounded-md border border-border-strong text-ink-2 hover:bg-bg-inset disabled:opacity-50"
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
        >
          {previewUrl ? '替换' : '上传'}
        </button>
        {previewUrl && (
          <button
            className="text-[12px] font-semibold px-2.5 py-1.5 rounded-md border border-danger text-danger hover:bg-danger-soft"
            onClick={() => setConfirmOpen(true)}
          >
            删除
          </button>
        )}
        {confirmOpen && (
          <ConfirmDialog
            title="确定要删除这张照片吗？"
            message="删除后会自动回退到自动查图结果（如果有）或占位框，不影响其他内容。"
            confirmLabel="确定删除"
            onCancel={() => setConfirmOpen(false)}
            onConfirm={() => {
              setConfirmOpen(false)
              onDelete()
            }}
          />
        )}
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  )
}
