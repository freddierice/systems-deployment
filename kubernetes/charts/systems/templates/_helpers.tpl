{{- define "systems.webApps" -}}
{{- $apps := deepCopy .Values.apps -}}
{{- if and .Values.dailyReport.enabled .Values.dailyReport.web.enabled -}}
{{- $_ := set $apps "time" (dict "enabled" true "hostname" .Values.dailyReport.web.hostname) -}}
{{- end -}}
{{- toYaml $apps -}}
{{- end -}}
