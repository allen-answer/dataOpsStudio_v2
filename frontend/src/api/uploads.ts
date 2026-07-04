/**
 * 2.3.0 上传件 API client(L-2 批量血缘 / compare 上传源共用)。
 *
 * ★ 字段形状锚后端契约,不臆造:
 *  - app/api/schemas.py:UploadPurpose / UploadResponse
 *  - app/api/routes/core.py POST /projects/{pid}/uploads?purpose=&filename=
 *    raw body 直传(非 multipart);100MB 超限 413 upload_too_large,
 *    空 body 400 empty_upload。
 */
import { apiClient } from './client'

export type UploadPurpose = 'lineage_batch' | 'compare_source'

export interface UploadResponse {
  upload_id: string
  project_id: string
  purpose: UploadPurpose
  filename: string
  content_type: string
  bytes: number
  created_at: string
}

/**
 * POST /projects/{pid}/uploads —— raw body 直传文件本体,
 * Content-Type 用文件真实类型(ZIP 常见 application/zip / x-zip-compressed)。
 */
export function uploadFile(
  projectId: string,
  file: File,
  purpose: UploadPurpose,
): Promise<UploadResponse> {
  const qs = new URLSearchParams({ purpose, filename: file.name })
  return apiClient.postRaw<UploadResponse>(
    `/projects/${projectId}/uploads?${qs.toString()}`,
    file,
    file.type || 'application/zip',
  )
}
