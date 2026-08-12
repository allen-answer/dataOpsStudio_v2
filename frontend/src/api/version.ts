import { apiClient } from './client'

export interface BuildInfo {
  version: string
  commit: string
  image_version: string
}

export function getBuildInfo(): Promise<BuildInfo> {
  return apiClient.get<BuildInfo>('/version')
}
