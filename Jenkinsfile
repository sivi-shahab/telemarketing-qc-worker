// GANTI SEBELUM DIPAKAI: REGISTRY (+ credentialsId 'gitlab-registry' di Jenkins).
//
// PENTING: kalau rilis mengandung perubahan skema, job API (yang menjalankan
// migrasi) harus selesai LEBIH DULU. Atur lewat upstream trigger atau
// `lock('qc-release')` di kedua job.
pipeline {
  agent any

  environment {
    IMAGE    = "qc-worker"
    REGISTRY = "registry.gitlab.<domain>/<group>"
    TAG      = "${env.GIT_COMMIT.take(8)}"
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
        sh 'git submodule update --init --recursive'
      }
    }

    stage('Import Check') {
      // Worker tidak punya test suite sendiri; minimal pastikan seluruh task
      // bisa di-import (dependensi lengkap, tidak ada sisa import ke `api`).
      steps {
        sh '''
          python3 -m venv .venv
          . .venv/bin/activate
          pip install --upgrade pip
          pip install -r core/requirements.txt -r worker/requirements.txt
          PYTHONPATH=.:core python -c "from worker.celery_app import celery_app; import worker.tasks.process_transcript, worker.tasks.process_document"
        '''
      }
    }

    stage('No API Import') {
      steps {
        sh '! grep -rn --include=*.py -E "^[[:space:]]*(from|import)[[:space:]]+api\\b" worker'
      }
    }

    stage('Build') {
      steps {
        sh "docker build -f worker/Dockerfile -t ${REGISTRY}/${IMAGE}:${TAG} -t ${REGISTRY}/${IMAGE}:latest ."
      }
    }

    stage('Push') {
      steps {
        withCredentials([usernamePassword(credentialsId: 'gitlab-registry',
                                          usernameVariable: 'U', passwordVariable: 'P')]) {
          sh '''
            echo "$P" | docker login ${REGISTRY} -u "$U" --password-stdin
            docker push ${REGISTRY}/${IMAGE}:${TAG}
            docker push ${REGISTRY}/${IMAGE}:latest
          '''
        }
      }
    }

    stage('Deploy') {
      when { branch 'main' }
      // Satu image, dua service.
      steps { sh 'docker compose up -d --no-deps worker flower' }
    }
  }
}
