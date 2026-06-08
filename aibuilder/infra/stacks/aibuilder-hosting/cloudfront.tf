resource "aws_cloudfront_distribution" "aibuilder" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${local.name} — HTTPS front door for the ALB"
  price_class     = "PriceClass_100" # US + EU edges only — cheaper for a small audience

  origin {
    domain_name = aws_lb.aibuilder.dns_name
    origin_id   = "alb"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only" # CloudFront → ALB is HTTP inside AWS
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]

    # No caching of dynamic content. The chat is fully personalised.
    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0

    # Forward the headers the FastAPI middleware needs.
    forwarded_values {
      query_string = true
      headers      = ["Authorization", "Host", "Content-Type"]

      cookies {
        forward = "none"
      }
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # *.cloudfront.net cert, free
  }
}

output "cloudfront_domain" {
  value       = "https://${aws_cloudfront_distribution.aibuilder.domain_name}"
  description = "Public HTTPS URL for aibuilder. Visit this in a browser."
}
