plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "io.github.naod6473.hermestv"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.naod6473.hermestv"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    // One shared key, committed on purpose: every build (yours, CI's) can update the app already
    // installed on the TV. It only protects updates of a sideloaded hobby app; use your own key for
    // anything else (see android-tv/README.md).
    signingConfigs {
        create("shared") {
            storeFile = file("hermes-tv.keystore")
            storePassword = "hermes-tv"
            keyAlias = "hermes-tv"
            keyPassword = "hermes-tv"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("shared")
        }
        debug {
            signingConfig = signingConfigs.getByName("shared")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}
