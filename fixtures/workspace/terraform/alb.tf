resource "aws_lb_target_group" "maven_service" {
  name = "demo-maven-service"
  health_check = {
    path                = "/actuator/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}
