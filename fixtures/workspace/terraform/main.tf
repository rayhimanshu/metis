resource "aws_s3_bucket" "images" {
  bucket = "demo-images"
}

resource "aws_db_instance" "main" {
  identifier = "demo-db"
  engine     = "postgres"
}

data "aws_iam_policy_document" "backend_s3" {
  statement {
    sid    = "S3ObjectAccess"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject"
    ]
    resources = ["${aws_s3_bucket.images.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.images.arn]
  }
}
