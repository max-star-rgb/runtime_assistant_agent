// 在京东搜索结果页提取商品列表，并与目标标题做模糊匹配
// 参数 targetTitle 通过引擎变量替换注入
(function(targetTitle) {
  // 检测是否被风控拦截
  if (document.title === '京东验证' || window.location.href.indexOf('risk_handler') >= 0) {
    return { error: 'captcha', message: '京东触发了验证码，需要手动完成验证' };
  }

  var items = [];
  var skuEls = document.querySelectorAll('[data-sku]');
  for (var i = 0; i < Math.min(skuEls.length, 10); i++) {
    var el = skuEls[i];
    var titleEl = el.querySelector('[title]');
    var t = titleEl ? (titleEl.getAttribute('title') || '').trim() : '';
    var sku = el.getAttribute('data-sku');
    if (t && sku) {
      items.push({ title: t, url: 'https://item.jd.com/' + sku + '.html' });
    }
  }
  if (!items.length) return { error: 'no_results', message: '未找到商品，可能页面未加载完成' };

  // 归一化：去除空格、符号，转小写
  function norm(s) {
    return s.replace(/[\s\-\/【】\(\)（）《》\[\]]/g, '').toLowerCase();
  }

  var target = norm(targetTitle);
  var best = items[0];
  var bestScore = 0;

  for (var j = 0; j < items.length; j++) {
    var s = norm(items[j].title);
    var score = 0;
    for (var k = 0; k < target.length; k++) {
      if (s.indexOf(target[k]) >= 0) score++;
    }
    score = score / Math.max(target.length, 1);
    if (score > bestScore) {
      bestScore = score;
      best = items[j];
    }
  }
  return best;
})('{keyword}')
