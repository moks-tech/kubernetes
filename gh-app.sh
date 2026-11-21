R=mrjithendar/k8s-deployments
KEY=$(ls -t ~/Downloads/mrjithendar-app.*.private-key.pem | head -1)
gh secret set RUNNERS_GITHUB_APP_ID              --repo $R --env mk4 --body 5858881
gh secret set RUNNERS_GITHUB_APP_INSTALLATION_ID --repo $R --env mk4 --body 184254779
gh secret set RUNNERS_GITHUB_APP_PRIVATE_KEY     --repo $R --env mk4 < "$KEY"


cd ~/Desktop/Work/k8s-deployments
git pull
export DOCKER_CONFIG=$(mktemp -d)
export TF_VAR_github_runners_app_id=5858881
export TF_VAR_github_runners_app_installation_id=184254779
export TF_VAR_github_runners_app_private_key="$(cat "$KEY")"
terraform -chdir=minikube-infrastructure apply -var-file=vars/mk4.tfvars -target=helm_release.arc_runners


# kubectl -n runners-system get pods 