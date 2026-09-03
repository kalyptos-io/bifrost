{{- define "bifrost.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "bifrost.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "bifrost.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "bifrost.labels" -}}
helm.sh/chart: {{ include "bifrost.chart" . }}
{{ include "bifrost.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "bifrost.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bifrost.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* demo ui: distinct name (so the api Service never selects ui pods) + component label */}}
{{- define "bifrost.ui.fullname" -}}
{{- printf "%s-ui" (include "bifrost.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "bifrost.ui.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bifrost.name" . }}-ui
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: ui
{{- end -}}

{{- define "bifrost.ui.labels" -}}
helm.sh/chart: {{ include "bifrost.chart" . }}
{{ include "bifrost.ui.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "bifrost.docs.fullname" -}}
{{- printf "%s-docs" (include "bifrost.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "bifrost.docs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bifrost.name" . }}-docs
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: docs
{{- end -}}

{{- define "bifrost.docs.labels" -}}
helm.sh/chart: {{ include "bifrost.chart" . }}
{{ include "bifrost.docs.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* sync worker: keeps name=bifrost (external Cilium egress keys on name only) but a distinct instance
     so the api Service selector {name,instance} never matches the probe-less worker pod. */}}
{{- define "bifrost.sync.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bifrost.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}-sync
app.kubernetes.io/component: sync
{{- end -}}

{{- define "bifrost.sync.labels" -}}
helm.sh/chart: {{ include "bifrost.chart" . }}
{{ include "bifrost.sync.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "bifrost.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "bifrost.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* sync is its own release unit: it owns an account rather than referencing the app plane's */}}
{{- define "bifrost.sync.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- printf "%s-sync" (include "bifrost.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "bifrost.secretName" -}}
{{- if .Values.database.existingSecret -}}
{{- .Values.database.existingSecret -}}
{{- else -}}
{{- include "bifrost.fullname" . -}}
{{- end -}}
{{- end -}}

{{/* sync dsn secret: a dedicated privileged secret if given, else the app secret (dev only). */}}
{{- define "bifrost.syncSecretName" -}}
{{- default (include "bifrost.secretName" .) .Values.sync.database.existingSecret -}}
{{- end -}}

{{- define "bifrost.syncSecretKey" -}}
{{- default .Values.database.existingSecretKey .Values.sync.database.existingSecretKey -}}
{{- end -}}

{{/* effective max replicas, for spread/pdb gating */}}
{{- define "bifrost.maxReplicas" -}}
{{- if .Values.autoscaling.enabled -}}
{{- .Values.autoscaling.maxReplicas -}}
{{- else -}}
{{- .Values.replicaCount -}}
{{- end -}}
{{- end -}}

{{/* uvicorn workers: explicit, else 1-per-core from a whole-core limits.cpu (cores or clean milli). */}}
{{- define "bifrost.workers" -}}
{{- if .Values.workers -}}
{{- .Values.workers -}}
{{- else -}}
{{- $cpu := (.Values.resources.limits).cpu -}}
{{- if not $cpu -}}
{{- fail "set resources.limits.cpu (workers derive from it) or pin an explicit workers value" -}}
{{- end -}}
{{- $cpu = $cpu | toString -}}
{{- $milli := ternary (trimSuffix "m" $cpu | int) (mulf ($cpu | float64) 1000 | int) (hasSuffix "m" $cpu) -}}
{{- if or (lt $milli 1000) (ne (mod $milli 1000) 0) -}}
{{- fail (printf "resources.limits.cpu=%v must be a whole number of cores when workers is unset (e.g. 2, 2.0 or 2000m - not 1.5/1500m/250m); pin workers for fractional cpu" $cpu) -}}
{{- end -}}
{{- div $milli 1000 -}}
{{- end -}}
{{- end -}}
