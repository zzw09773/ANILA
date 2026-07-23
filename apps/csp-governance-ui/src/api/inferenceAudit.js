import client from './client'
import { buildInferenceAuditParams } from '../utils/inferenceAudit'

export const listInferenceAudit = (filters) =>
  client.get('/api/admin/audit/inference', {
    params: buildInferenceAuditParams(filters, { includePagination: true }),
  })

export const exportInferenceAuditCsv = (filters) =>
  client.get('/api/admin/audit/inference/export', {
    params: buildInferenceAuditParams(filters, { includePagination: false }),
    responseType: 'blob',
  })
