import client from './client'

export const listSkillReviews = () => client.get('/api/skills/reviews')
export const approveSkillReview = (versionId) => client.post(`/api/skills/reviews/${versionId}/approve`)
export const rejectSkillReview = (versionId, reason) => (
  client.post(`/api/skills/reviews/${versionId}/reject`, { reason })
)
export const unpublishSkillReview = (versionId) => client.post(`/api/skills/reviews/${versionId}/unpublish`)
