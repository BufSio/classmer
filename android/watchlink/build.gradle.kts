// Lo que comparten la app del celu (:app) y la del Galaxy Watch (:wear): el protocolo del Data Layer,
// la transferencia del audio por tramas y la cuenta del desfase entre relojes. Kotlin puro (sin
// Android), así se prueba rápido en la JVM.
plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    api(libs.kotlinx.serialization.json)
    testImplementation(libs.junit)
}
