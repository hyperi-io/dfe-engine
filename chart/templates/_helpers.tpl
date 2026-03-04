{{/*
Expand the name of the chart.
*/}}
{{- define "dfe-engine.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "dfe-engine.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart label.
*/}}
{{- define "dfe-engine.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "dfe-engine.labels" -}}
helm.sh/chart: {{ include "dfe-engine.chart" . }}
{{ include "dfe-engine.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "dfe-engine.selectorLabels" -}}
app.kubernetes.io/name: {{ include "dfe-engine.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "dfe-engine.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "dfe-engine.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Secret name for ClickHouse credentials.
*/}}
{{- define "dfe-engine.clickhouseSecretName" -}}
{{- if .Values.clickhouse.existingSecret }}
{{- .Values.clickhouse.existingSecret }}
{{- else }}
{{- include "dfe-engine.fullname" . }}
{{- end }}
{{- end }}

{{/*
Secret name for JWT.
*/}}
{{- define "dfe-engine.jwtSecretName" -}}
{{- if .Values.jwt.existingSecret }}
{{- .Values.jwt.existingSecret }}
{{- else }}
{{- include "dfe-engine.fullname" . }}
{{- end }}
{{- end }}

{{/*
Secret name for auth passwords.
*/}}
{{- define "dfe-engine.authSecretName" -}}
{{- if .Values.auth.admin.existingSecret }}
{{- .Values.auth.admin.existingSecret }}
{{- else }}
{{- include "dfe-engine.fullname" . }}
{{- end }}
{{- end }}
