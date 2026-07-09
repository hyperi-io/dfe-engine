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

{{/*
ClickHouse connection env vars - shared by the hunt-runner deployment + the
materialise Job (the runner coordinates entirely through ClickHouse).
*/}}
{{- define "dfe-engine.clickhouseEnv" -}}
- name: DFE_CLICKHOUSE_HOST
  value: {{ .Values.config.clickhouse.host | quote }}
- name: DFE_CLICKHOUSE_PORT
  value: {{ .Values.config.clickhouse.port | quote }}
- name: DFE_CLICKHOUSE_USERNAME
  value: {{ .Values.config.clickhouse.username | quote }}
- name: DFE_CLICKHOUSE_DATABASE
  value: {{ .Values.config.clickhouse.database | quote }}
{{- with .Values.config.clickhouse.data_database }}
- name: DFE_CLICKHOUSE_DATA_DATABASE
  value: {{ . | quote }}
{{- end }}
- name: DFE_CLICKHOUSE_SECURE
  value: {{ .Values.config.clickhouse.secure | quote }}
- name: DFE_CLICKHOUSE_VERIFY
  value: {{ .Values.config.clickhouse.verify | quote }}
- name: DFE_CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "dfe-engine.clickhouseSecretName" . }}
      key: {{ .Values.clickhouse.secretKeys.password }}
{{- end }}

{{/*
Config-dir env vars (hunt configs read from the mounted gitops config).
*/}}
{{- define "dfe-engine.configDirEnv" -}}
{{- if .Values.config.config_dir }}
- name: DFE_CONFIG_DIR
  value: {{ .Values.config.config_dir | quote }}
{{- else if .Values.configVolume.enabled }}
- name: DFE_CONFIG_DIR
  value: {{ .Values.configVolume.mountPath | quote }}
{{- end }}
{{- with .Values.huntRunner.huntsDir }}
- name: DFE_HUNTS_DIR
  value: {{ . | quote }}
{{- end }}
{{- end }}
