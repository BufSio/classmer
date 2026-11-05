package ar.classmer.util

import kotlinx.serialization.json.Json

/** Configuración JSON común: tolera campos nuevos del server y escribe los nulls explícitos. */
val JsonClassmer = Json {
    ignoreUnknownKeys = true
    encodeDefaults = true
    explicitNulls = true
    coerceInputValues = true
}
