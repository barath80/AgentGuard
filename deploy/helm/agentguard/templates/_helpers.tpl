{{- define "agentguard.name" -}}{{ .Chart.Name }}{{- end -}}

{{- define "agentguard.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "agentguard.labels" -}}
app.kubernetes.io/name: {{ include "agentguard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Selector labels for one component: include with (dict "ctx" . "component" "gateway") */}}
{{- define "agentguard.selector" -}}
app.kubernetes.io/name: {{ include "agentguard.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "agentguard.image" -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end -}}

{{- define "agentguard.helpdeskUrl" -}}
{{- if .Values.helpdesk.enabled -}}http://{{ include "agentguard.fullname" . }}-helpdesk:8200
{{- else -}}{{ required "helpdesk.targetUrl is required when helpdesk.enabled is false" .Values.helpdesk.targetUrl }}{{- end -}}
{{- end -}}

{{/* Settings shared by every process that reads the AgentGuard database. */}}
{{- define "agentguard.commonEnv" -}}
- name: ENVIRONMENT
  value: {{ .Values.environment | quote }}
- name: DATABASE_URL
  valueFrom: { secretKeyRef: { name: {{ .Values.database.existingSecret }}, key: DATABASE_URL } }
- name: TARGET_BACKEND
  value: helpdesk
- name: BROKER_TIMEOUT_SECONDS
  value: {{ .Values.broker.timeoutSeconds | quote }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.observability.otlpEndpoint | quote }}
{{- end -}}

{{- define "agentguard.tmpVolume" -}}
- name: tmp
  emptyDir: { sizeLimit: 64Mi }
{{- end -}}
