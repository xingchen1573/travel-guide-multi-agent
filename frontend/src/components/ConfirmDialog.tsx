import { Card } from './Card'

export function ConfirmDialog({
  title,
  message,
  confirmLabel = '确定',
  danger = true,
  onConfirm,
  onCancel,
}: {
  title: string
  message: string
  confirmLabel?: string
  danger?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" role="dialog" aria-modal="true">
      <Card className="p-6 max-w-sm mx-4">
        <h3 className="font-bold text-base mb-2">{title}</h3>
        <p className="text-sm text-ink-2 mb-5">{message}</p>
        <div className="flex justify-end gap-2.5">
          <button
            className="px-4 py-2 rounded-lg text-[13px] font-semibold border border-border-strong text-ink-2 hover:bg-bg-inset"
            onClick={onCancel}
          >
            取消
          </button>
          <button
            className={`px-4 py-2 rounded-lg text-[13px] font-semibold text-white hover:opacity-90 ${
              danger ? 'bg-danger' : 'bg-brand'
            }`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </Card>
    </div>
  )
}
