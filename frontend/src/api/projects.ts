import { apiClient } from './client'
import type { Project } from './types'

export function listProjects(): Promise<Project[]> {
  return apiClient.get<Project[]>('/projects')
}
